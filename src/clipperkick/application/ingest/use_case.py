"""Caso de uso de ingesta: de una entrada del mundo a un artefacto reutilizable.

No conoce Qt, ni Tkinter, ni ffprobe, ni SQLite. Lo unico que decide es el
**orden**, y el orden es la mitad del ticket:

```text
materializar bytes -> huella -> ¿ya conocido? -> clave -> ¿artefacto valido?
        -> sondear y publicar -> registrar en SQLite
```

Tres decisiones de orden merecen explicacion, porque cada una tapa un fallo que
la version obvia deja abierto:

1. **SQLite se toca al final.** Sondear antes de registrar significa que una
   fuente que ffprobe no sabe describir no deja fila ninguna: no hay que
   inventar un estado "registrada pero indescribible" ni limpiarlo despues. Los
   bytes que se hubieran copiado al proyecto se retiran en el mismo camino de
   error.
2. **La publicacion precede al registro, y eso es correcto.** Si el proceso
   muere entre ambos, quedan bytes publicados sin fila. La siguiente ingesta
   recalcula la misma clave, encuentra la publicacion, la *adopta* —el almacen
   de T03 comprueba identidad, linaje y bytes antes de aceptarla— y registra
   entonces. Al reves —fila primero— el proyecto afirmaria tener un artefacto
   que nadie puede encontrar.
3. **La reutilizacion se decide sobre bytes, no sobre la fila.** Ni una fila
   `available`, ni la mera *presencia* de un archivo donde la fila dice, prueban
   nada: lo unico que prueba que unos bytes son los que se creen es volver a
   digerirlos. Por eso un hit por identidad verifica la huella real del destino
   registrado antes de reutilizarlo, y por eso la lectura del artefacto vuelve
   verificada del almacen en vez de abrirse otra vez.

4. **Los bytes que esta ingesta deposita tienen dueno en un instante concreto.**
   Antes de que ninguna fila los referencie, un fallo se los lleva; despues, no
   se tocan aunque falle cualquier otra cosa. El punto exacto es el commit del
   repositorio, y por eso lo marca quien lo ejecuta y no el bloque que lo rodea.

La limpieza de temporales abandonados corre al principio y con un umbral de
antiguedad. Un proyecto tiene un unico escritor, de modo que nada vivo puede
estar usando un staging viejo; el umbral existe para que un reloj mal ajustado
no convierta la limpieza en una carrera contra la ingesta en curso.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import uuid

from clipperkick.domain.ingest import (
    CODIGO_ARTEFACTO_PUBLICADO, CODIGO_ARTEFACTO_REUTILIZADO, CODIGO_AVISO_MEDIA,
    CODIGO_COPIA_COMPLETA, CODIGO_DESCARGA_CANCELADA, CODIGO_DESCARGA_COMPLETA,
    CODIGO_DESCENDIENTES_INVALIDADOS,
    CODIGO_FUENTE_REEMPLAZADA, CODIGO_FUENTE_REGISTRADA, CODIGO_FUENTE_RELOCALIZADA,
    CODIGO_FUENTE_REPARADA, CODIGO_FUENTE_REUTILIZADA, CODIGO_ORIGEN_NO_RETIRADO,
    CODIGO_SONDEO_COMPLETO, CODIGO_TEMPORALES_RETIRADOS,
    VERSION_METADATA, DiagnosticoIngesta, ErrorArtefactoIngesta, ErrorCancelacionIngesta,
    ErrorDescargaIngesta, ErrorFuenteIngesta, ErrorHuellaIngesta, ErrorIngesta, HuellaFuente,
    LinajeArtefacto,
    ManifiestoArtefacto, MetadataMedia, ModoFuente, OrigenFuente, RegistroArtefacto, SourceAsset,
    canonicalizar, clave_materializacion, detecta_reemplazo, normalizar_sondeo, reutilizable,
    sondeo_canonico,
)

from .ports import (
    ANTIGUEDAD_TEMPORALES, NOMBRE_ARCHIVO_SONDEO, NOMBRE_STAGE_SONDEO, TIPO_ARTEFACTO_SONDEO,
    VERSION_CONTRATO_SONDEO, AlmacenArtefactosIngesta, AlmacenFuentes, Cancelacion,
    DescargadorFuente, Notificador, RepositorioFuentes, ResultadoIngesta, ServicioHuellas,
    SolicitudIngesta, SondaDetallada, UbicacionFuente, nunca_cancelado, sin_notificar,
)


#: Formato del documento publicado por la etapa de sondeo. El documento guarda
#: el sondeo **crudo** ademas de la metadata normalizada: la normalizacion es
#: una regla versionada del dominio, y conservar su entrada permite rederivarla
#: sin volver a tocar el archivo original.
FORMATO_SONDEO = "clipsapp-sondeo/1"

ESQUEMAS_REMOTOS = ("http://", "https://")

#: Veredictos posibles sobre la copia que una fila ya registrada dice tener.
VERIFICACION_COINCIDE = "coincide"
VERIFICACION_AUSENTE = "ausente"
VERIFICACION_DISCREPA = "discrepa"


def es_entrada_remota(entrada: str) -> bool:
    return entrada.lower().startswith(ESQUEMAS_REMOTOS)


class _Propiedad:
    """De quien son los bytes que esta ingesta acaba de depositar.

    Nace en manos del caso de uso —nadie los referencia todavia— y se cede en el
    instante exacto en que un commit del repositorio los reclama. Es un objeto y
    no una bandera local porque quien cede la propiedad es el metodo que ejecuta
    el commit, y quien la consulta es el bloque de error que lo envuelve: con
    una variable, un fallo *despues* del commit encontraria el valor viejo y
    borraria el material de una fuente que el proyecto ya afirma tener.

    Una fuente referenciada nace cedida: sus bytes viven fuera del proyecto y
    esta capa no los toca jamas, ni siquiera para limpiar.
    """

    def __init__(self, relativa: str | None) -> None:
        self.relativa = relativa
        self.cedida = relativa is None

    def ceder(self) -> None:
        self.cedida = True

    @property
    def retirable(self) -> str | None:
        return None if self.cedida else self.relativa


@dataclass(frozen=True)
class _Resolucion:
    """Como quedo la fuente tras conciliar lo entrante con lo ya registrado."""

    fuente: SourceAsset
    reutilizada: bool = False
    reemplazada: str | None = None
    reparada: str | None = None
    invalidados: tuple[str, ...] = ()


class CasoDeUsoIngesta:
    """Convierte una ruta local o una URL en `SourceAsset` + sondeo publicado."""

    def __init__(self, *, almacen_fuentes: AlmacenFuentes, huellas: ServicioHuellas,
                 sonda: SondaDetallada, artefactos: AlmacenArtefactosIngesta,
                 repositorio: RepositorioFuentes,
                 descargador: DescargadorFuente | None = None,
                 notificar: Notificador = sin_notificar,
                 antiguedad_temporales: float = ANTIGUEDAD_TEMPORALES) -> None:
        self._fuentes = almacen_fuentes
        self._huellas = huellas
        self._sonda = sonda
        self._artefactos = artefactos
        self._repositorio = repositorio
        self._descargador = descargador
        self._notificar = notificar
        self._antiguedad = float(antiguedad_temporales)

    # ------------------------------------------------------------------ #
    # API publica
    # ------------------------------------------------------------------ #

    def ingerir(self, solicitud: SolicitudIngesta,
                cancelado: Cancelacion | None = None) -> ResultadoIngesta:
        vigilar = cancelado if cancelado is not None else nunca_cancelado
        diagnosticos: list[DiagnosticoIngesta] = []
        retirados = (self._limpiar(diagnosticos) if solicitud.limpiar_temporales else ())
        entrada = self._exigir_entrada(solicitud.entrada)
        self._exigir_vigente(vigilar)

        ubicacion, modo, origen, uri = self._materializar(
            entrada, solicitud, diagnosticos, vigilar)
        propiedad = _Propiedad(ubicacion.ruta_relativa)
        try:
            # Antes de gastar una pasada completa de hashing sobre un archivo
            # que puede ocupar gigabytes.
            self._exigir_vigente(vigilar)
            huella = self._huellas.completa(ubicacion.ruta_absoluta)
            existente = self._repositorio.buscar_por_identidad(huella.identidad)
            clave = self._clave(huella)
            linaje = self._linaje(clave, huella)

            # Ultimo punto de cancelacion. A partir de aqui puede haber bytes
            # publicados, y dejarlos sin registrar es peor que terminar: la
            # publicacion ya es durable y el registro es lo que la explica.
            self._exigir_vigente(vigilar)
            metadata, rutas_artefacto, reutilizado = self._materializar_sondeo(
                clave, linaje, huella, ubicacion.ruta_absoluta, diagnosticos)

            resolucion = self._resolver_fuente(
                existente, huella, modo, origen, uri, ubicacion, solicitud, diagnosticos,
                propiedad)
            self._repositorio.registrar_artefacto(RegistroArtefacto(
                clave=clave, tipo=TIPO_ARTEFACTO_SONDEO, rutas=rutas_artefacto,
                checksums=self._artefactos.presentes(clave), linaje=linaje,
                fuente_id=resolucion.fuente.id))
        except BaseException:
            # Solo se retira lo que esta capa deposito y **nadie** reclamo aun.
            retirable = propiedad.retirable
            if retirable:
                self._fuentes.retirar(retirable)
            raise

        fuente = resolucion.fuente
        for aviso in sorted(aviso.value for aviso in metadata.avisos):
            self._emitir(diagnosticos, CODIGO_AVISO_MEDIA, {"aviso": aviso, "fuente": fuente.id})
        return ResultadoIngesta(
            fuente=fuente, metadata=metadata, clave=clave,
            artefactos=tuple(f"artifacts/{clave}/{ruta}" for ruta in rutas_artefacto),
            fuente_reutilizada=resolucion.reutilizada, artefacto_reutilizado=reutilizado,
            fuente_reemplazada=resolucion.reemplazada, fuente_reparada=resolucion.reparada,
            invalidados=resolucion.invalidados,
            diagnosticos=tuple(diagnosticos), temporales_retirados=retirados)

    def _exigir_vigente(self, cancelado: Cancelacion) -> None:
        if cancelado():
            raise ErrorCancelacionIngesta("La ingesta se cancelo antes de publicar nada.")

    def limpiar_temporales(self) -> tuple[str, ...]:
        """Retira staging abandonado de ingestas y de sondeos. Idempotente."""
        return self._limpiar([])

    # ------------------------------------------------------------------ #
    # Materializacion de los bytes
    # ------------------------------------------------------------------ #

    def _exigir_entrada(self, entrada: object) -> str:
        if not isinstance(entrada, str) or not entrada.strip():
            raise ErrorFuenteIngesta("La ingesta necesita una ruta local o un enlace.")
        return entrada.strip()

    def _materializar(self, entrada: str, solicitud: SolicitudIngesta,
                      diagnosticos: list[DiagnosticoIngesta], cancelado: Cancelacion,
                      ) -> tuple[UbicacionFuente, ModoFuente, OrigenFuente, str]:
        if es_entrada_remota(entrada):
            return (*self._descargar(entrada, solicitud, diagnosticos, cancelado), entrada)
        normalizada = self._fuentes.normalizar(entrada)
        if solicitud.copiar_local:
            ubicacion = self._fuentes.incorporar(entrada, solicitud.nombre, mover=False)
            self._emitir(diagnosticos, CODIGO_COPIA_COMPLETA,
                         {"destino": ubicacion.ruta_relativa or ""})
            return ubicacion, ModoFuente.COPIADA, OrigenFuente.LOCAL, normalizada
        ubicacion = self._fuentes.referenciar(entrada)
        return ubicacion, ModoFuente.REFERENCIADA, OrigenFuente.LOCAL, normalizada

    def _descargar(self, url: str, solicitud: SolicitudIngesta,
                   diagnosticos: list[DiagnosticoIngesta], cancelado: Cancelacion,
                   ) -> tuple[UbicacionFuente, ModoFuente, OrigenFuente]:
        """Descarga a un temporal y solo mueve lo *terminado* a `sources/`.

        El archivo se mueve, no se copia: mientras vive en el staging no es una
        fuente, y si la descarga muere a medias el staging se retira entero. Una
        descarga incompleta no puede quedarse en `sources/` pareciendo una
        fuente valida a la que solo le faltan bytes.

        El token de cancelacion baja hasta el adaptador porque es el unico que
        puede atenderlo: comprobarlo aqui, alrededor de una llamada que puede
        durar horas, seria comprobarlo dos veces y nunca durante. El adaptador
        se compromete a devolver el control con el proceso hijo ya recolectado,
        de modo que el `staging()` que retira el directorio al salir no compite
        con un escritor vivo —cosa que en Windows ni siquiera seria posible—.
        """
        if self._descargador is None:
            raise ErrorDescargaIngesta(
                "Esta configuracion de ingesta no admite enlaces: falta el descargador.")
        with self._fuentes.staging() as temporal:
            try:
                descargado = self._descargador.descargar(
                    url, temporal, solicitud.preferir_hd, None, cancelado)
            except ErrorCancelacionIngesta:
                self._emitir(diagnosticos, CODIGO_DESCARGA_CANCELADA, {"entrada": url})
                raise
            ubicacion = self._fuentes.incorporar(descargado, solicitud.nombre, mover=True)
            if ubicacion.origen_pendiente:
                # El destino esta confirmado; lo que quedo es una copia del
                # temporal. Se cuenta en vez de callarlo, aunque el `staging()`
                # que se cierra a continuacion se la lleve por delante.
                self._emitir(diagnosticos, CODIGO_ORIGEN_NO_RETIRADO,
                             {"origen": ubicacion.origen_pendiente})
        self._emitir(diagnosticos, CODIGO_DESCARGA_COMPLETA,
                     {"destino": ubicacion.ruta_relativa or "", "hd": solicitud.preferir_hd})
        return ubicacion, ModoFuente.COPIADA, OrigenFuente.DESCARGA

    # ------------------------------------------------------------------ #
    # Clave, linaje y artefacto
    # ------------------------------------------------------------------ #

    def _clave(self, huella: HuellaFuente) -> str:
        """Clave del sondeo: entrada, contrato de normalizacion y proveedor.

        `VERSION_METADATA` viaja dentro de la version de contrato porque cambiar
        la normalizacion cambia el artefacto aunque el archivo y ffprobe sean
        los mismos: sin ella, un proyecto viejo reutilizaria un `probe.json`
        escrito con reglas que ya no rigen.
        """
        return clave_materializacion(
            stage=NOMBRE_STAGE_SONDEO,
            version_contrato=f"{VERSION_CONTRATO_SONDEO}/{VERSION_METADATA}",
            version_proveedor=self._sonda.version(),
            entradas={"fuente": huella.identidad})

    def _linaje(self, clave: str, huella: HuellaFuente) -> LinajeArtefacto:
        return LinajeArtefacto(
            stage=NOMBRE_STAGE_SONDEO,
            version_contrato=f"{VERSION_CONTRATO_SONDEO}/{VERSION_METADATA}",
            version_proveedor=self._sonda.version(), clave=clave,
            entradas={"fuente": huella.identidad})

    def _materializar_sondeo(self, clave: str, linaje: LinajeArtefacto, huella: HuellaFuente,
                             ruta: str, diagnosticos: list[DiagnosticoIngesta],
                             ) -> tuple[MetadataMedia, tuple[str, ...], bool]:
        registro = self._repositorio.artefacto(clave)
        if registro is not None and reutilizable(registro, linaje, self._artefactos.presentes(clave)):
            documento = self._leer_reutilizable(registro, clave)
            if documento is not None:
                metadata = normalizar_sondeo(self._sondeo_de(documento))
                self._emitir(diagnosticos, CODIGO_ARTEFACTO_REUTILIZADO, {"clave": clave})
                return metadata, tuple(registro.rutas), True
        return (*self._producir_sondeo(clave, linaje, huella, ruta, diagnosticos), False)

    def _leer_reutilizable(self, registro: RegistroArtefacto,
                           clave: str) -> Mapping[str, object] | None:
        """Lee el artefacto **verificando los mismos bytes** que se parsean.

        Comprobar los digests con `presentes()` y despues volver a abrir el
        archivo son dos lecturas: entre ellas cabe una escritura, y lo que se
        acabaria interpretando no serian los bytes aprobados. `leer_verificado`
        hace las dos cosas sobre un unico `bytes`.

        Un fallo aqui no es un error de la ingesta: significa que el artefacto
        dejo de servir entre la comprobacion y la lectura. Se devuelve `None` y
        el sondeo se vuelve a producir, que es exactamente lo que habria pasado
        si el artefacto no hubiese estado.
        """
        try:
            return self._artefactos.leer_verificado(
                clave, NOMBRE_ARCHIVO_SONDEO, registro.checksums.get(NOMBRE_ARCHIVO_SONDEO, ""))
        except (ErrorArtefactoIngesta, ErrorIngesta):
            return None

    def _sondeo_de(self, documento: Mapping[str, object]) -> Mapping[str, object]:
        """El sondeo crudo es la autoridad; `metadata` es una vista derivada.

        Se rederiva en vez de leerse para que no existan dos formas de obtener
        la misma metadata: una que aplica las reglas vigentes y otra que confia
        en lo que alguien escribio. Ambas viven en el mismo archivo firmado, de
        modo que no pueden divergir, pero solo una es la regla.
        """
        crudo = documento.get("sondeo") if isinstance(documento, Mapping) else None
        if not isinstance(crudo, Mapping):
            raise ErrorIngesta("El artefacto de sondeo publicado no conserva su documento.")
        return crudo

    def _producir_sondeo(self, clave: str, linaje: LinajeArtefacto, huella: HuellaFuente,
                         ruta: str, diagnosticos: list[DiagnosticoIngesta],
                         ) -> tuple[MetadataMedia, tuple[str, ...]]:
        directorio = self._artefactos.preparar(clave)
        try:
            crudo = sondeo_canonico(self._sonda.describir(ruta))
            metadata = normalizar_sondeo(crudo)
            documento = canonicalizar({
                "formato": FORMATO_SONDEO,
                "linaje": linaje.como_documento(),
                "entrada": {"huella": huella.como_documento()},
                "sondeo": crudo,
                "metadata": metadata.como_documento(),
            }).encode("utf-8")
            archivo = self._artefactos.escribir(directorio, NOMBRE_ARCHIVO_SONDEO, documento)
            manifiesto = ManifiestoArtefacto(
                tipo=TIPO_ARTEFACTO_SONDEO, archivos=(archivo,), linaje=linaje,
                metadata={"version_metadata": VERSION_METADATA})
            self._artefactos.publicar(clave, directorio, manifiesto)
        except BaseException:
            # El temporal se aparta, no se borra: es la unica evidencia de por
            # que un sondeo fallo, y dejarlo donde estaba haria que el siguiente
            # intento lo confundiese con trabajo bueno.
            self._artefactos.cuarentena(directorio)
            raise
        self._emitir(diagnosticos, CODIGO_SONDEO_COMPLETO,
                     {"clave": clave, "avisos": sorted(a.value for a in metadata.avisos)})
        self._emitir(diagnosticos, CODIGO_ARTEFACTO_PUBLICADO,
                     {"clave": clave, "archivos": [archivo.ruta]})
        return metadata, (archivo.ruta,)

    # ------------------------------------------------------------------ #
    # Fuente: reutilizacion, alta y reemplazo
    # ------------------------------------------------------------------ #

    def _verificar_registrada(self, existente: SourceAsset) -> str:
        """¿La copia que la fila dice tener contiene todavia *sus* bytes?

        Presencia no es identidad. Un archivo en la ruta registrada solo prueba
        que hay algo ahi: si alguien lo sobrescribio, preguntar `¿existe?`
        contesta que si y la ingesta descartaria el material entrante —que es el
        correcto— para reutilizar una fila y unos artefactos construidos sobre
        otros bytes. La unica respuesta valida es volver a digerirlo.

        Cuesta una pasada completa sobre el archivo registrado. Es el precio de
        la pregunta: cualquier atajo barato —tamano, `mtime`— es exactamente lo
        que una sobrescritura del mismo tamano deja intacto.

        Una lectura imposible o inestable cuenta como discrepancia, no como
        coincidencia: no poder demostrar que son los mismos bytes y demostrar
        que lo son no son la misma cosa.
        """
        if not self._fuentes.disponible(existente):
            return VERIFICACION_AUSENTE
        try:
            actual = self._huellas.completa(self._fuentes.resolver(existente))
        except (ErrorHuellaIngesta, ErrorIngesta):
            return VERIFICACION_DISCREPA
        return (VERIFICACION_COINCIDE if not detecta_reemplazo(existente.huella, actual)
                else VERIFICACION_DISCREPA)

    def _resolver_fuente(self, existente: SourceAsset | None, huella: HuellaFuente,
                         modo: ModoFuente, origen: OrigenFuente, uri: str,
                         ubicacion: UbicacionFuente, solicitud: SolicitudIngesta,
                         diagnosticos: list[DiagnosticoIngesta],
                         propiedad: _Propiedad) -> _Resolucion:
        if existente is not None:
            return self._conciliar_existente(existente, huella, modo, ubicacion, diagnosticos,
                                             propiedad)

        nueva = SourceAsset(
            id=str(uuid.uuid4()),
            nombre=solicitud.nombre or ubicacion.nombre or uri,
            modo=modo, origen=origen, huella=huella,
            ruta_relativa=ubicacion.ruta_relativa,
            ruta_externa=None if modo is ModoFuente.COPIADA else ubicacion.ruta_absoluta,
            entrada_original=uri)
        anterior = self._repositorio.buscar_por_origen(uri)
        if anterior is not None and detecta_reemplazo(anterior.huella, huella):
            # La misma entrada trae otros bytes. Alta, jubilacion e invalidacion
            # son un unico commit del repositorio: repartidos, un fallo entre
            # medias deja dos filas vigentes para la misma entrada y los
            # descendientes viejos declarandose validos sobre contenido que ya
            # cambio. La fuente vieja no se borra: queda `replaced` con su
            # historia intacta y sus artefactos marcados reconstruibles, de modo
            # que momentos, drafts y revisiones siguen apuntando a ella.
            fuente, invalidados = self._repositorio.suceder(nueva, anterior.id)
            propiedad.ceder()
            self._emitir(diagnosticos, CODIGO_FUENTE_REEMPLAZADA,
                         {"anterior": anterior.id, "nueva": fuente.id, "entrada": uri})
            if invalidados:
                self._emitir(diagnosticos, CODIGO_DESCENDIENTES_INVALIDADOS,
                             {"fuente": anterior.id, "artefactos": list(invalidados)})
            return _Resolucion(fuente=fuente, reemplazada=anterior.id, invalidados=invalidados)

        fuente = self._repositorio.registrar(nueva)
        propiedad.ceder()
        self._emitir(diagnosticos, CODIGO_FUENTE_REGISTRADA,
                     {"fuente": fuente.id, "modo": modo.value, "origen": origen.value})
        return _Resolucion(fuente=fuente)

    def _conciliar_existente(self, existente: SourceAsset, huella: HuellaFuente,
                             modo: ModoFuente, ubicacion: UbicacionFuente,
                             diagnosticos: list[DiagnosticoIngesta],
                             propiedad: _Propiedad) -> _Resolucion:
        """Ya hay una fila con esta identidad. Lo que decide es que hay en disco."""
        veredicto = self._verificar_registrada(existente)
        externa = None if modo is ModoFuente.COPIADA else ubicacion.ruta_absoluta

        if veredicto == VERIFICACION_COINCIDE:
            # Los mismos bytes ya estan registrados y siguen siendo los mismos.
            # Si esta ingesta acabo de depositar una copia redundante dentro del
            # proyecto, se retira: es nuestra, nadie la referencia y conservarla
            # duplicaria el material sin que ninguna fila lo explicase.
            if ubicacion.ruta_relativa and ubicacion.ruta_relativa != existente.ruta_relativa:
                self._fuentes.retirar(ubicacion.ruta_relativa)
            propiedad.ceder()  # ya no queda nada que el camino de error deba retirar
            self._emitir(diagnosticos, CODIGO_FUENTE_REUTILIZADA,
                         {"fuente": existente.id, "identidad": huella.identidad})
            return _Resolucion(fuente=existente, reutilizada=True)

        if veredicto == VERIFICACION_AUSENTE:
            # Es el escenario "fuente externa movida" del plan: los bytes son los
            # suyos, solo que ya no estan donde decia. Se relocaliza en vez de
            # duplicar, de modo que el analisis y las ediciones que cuelgan de
            # ese id siguen sirviendo.
            relocalizada = self._repositorio.relocalizar(
                existente.id, modo.value, ubicacion.ruta_relativa, externa)
            propiedad.ceder()
            self._emitir(diagnosticos, CODIGO_FUENTE_RELOCALIZADA,
                         {"fuente": relocalizada.id, "modo": modo.value})
            return _Resolucion(fuente=relocalizada, reutilizada=True)

        # Discrepa: la fila declara una huella que su propia copia ya no tiene.
        # Lo entrante *si* la tiene —es como se encontro la fila—, asi que la
        # reparacion consiste en apuntar la fuente al material correcto. No se
        # reporta como reutilizacion: lo guardado estaba mal.
        #
        # La copia discrepante no se borra. Es la unica evidencia de que algo
        # sobrescribio material del proyecto, y decidir que se tira no es cosa de
        # una ingesta; queda sin referencias y se nombra en el diagnostico.
        reparada = self._repositorio.relocalizar(
            existente.id, modo.value, ubicacion.ruta_relativa, externa)
        propiedad.ceder()
        self._emitir(diagnosticos, CODIGO_FUENTE_REPARADA,
                     {"fuente": reparada.id, "identidad": huella.identidad,
                      "descartado": existente.ruta_relativa or existente.ruta_externa or ""})
        return _Resolucion(fuente=reparada, reparada=existente.id)

    # ------------------------------------------------------------------ #

    def _limpiar(self, diagnosticos: list[DiagnosticoIngesta]) -> tuple[str, ...]:
        retirados = (*self._fuentes.limpiar_abandonados(self._antiguedad),
                     *self._artefactos.limpiar_abandonados(self._antiguedad))
        if retirados:
            self._emitir(diagnosticos, CODIGO_TEMPORALES_RETIRADOS,
                         {"rutas": list(retirados)})
        return retirados

    def _emitir(self, diagnosticos: list[DiagnosticoIngesta], codigo: str,
                datos: Mapping[str, object]) -> None:
        diagnostico = DiagnosticoIngesta(codigo, datos)
        diagnosticos.append(diagnostico)
        self._notificar(diagnostico)
