"""Registro de etapas ejecutables y la etapa determinista de prueba.

`prueba` es infraestructura de producto, no un mock: el ticket pide una etapa
capaz de inyectar lentitud, crash, salida invalida, checkpoint y cancelacion
para poder ejercitar el coordinador con procesos reales. Todo su comportamiento
se deriva del payload y del checkpoint —no hay reloj ni azar dentro—, de modo
que dos ejecuciones con la misma entrada producen exactamente los mismos bytes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
import os
from pathlib import Path
import time

from clipperkick.domain.jobs import (
    ArchivoSalida, ErrorJob, ManifiestoSalida, MensajeWorker, PoliticaCancelacion, PoliticaRetry,
    Recursos, StageDefinition, TipoMensaje, exigir_componente_interno, exigir_ruta_interna,
)


#: Estado que debe sobrevivir a varios intentos del mismo job. Vive junto a los
#: directorios de intento, no dentro de uno, y sigue estando bajo `cache/`.
DIRECTORIO_ESTADO = "_estado"
NOMBRE_STAGE_PRUEBA = "prueba"
#: Misma etapa determinista declarada en otra clase de recurso. Existe porque
#: comprobar que las clases no compiten entre si exige al menos dos contratos
#: distintos, y el peso lo declara la `StageDefinition`, no el job.
NOMBRE_STAGE_PRUEBA_GPU = "prueba-gpu"
VERSION_STAGE_PRUEBA = "1"
CODIGO_TRANSITORIO_PRUEBA = "transitorio.io"
CODIGO_PERMANENTE_PRUEBA = "entrada.invalida"
RODAJA_ESPERA = 0.02  # cada cuanto se mira la cancelacion durante una espera


class FalloStage(ErrorJob):
    """Fallo declarado por una etapa, con el codigo que decide el reintento."""

    def __init__(self, codigo: str, mensaje: str = "") -> None:
        super().__init__(mensaje or codigo)
        self.codigo = codigo


class CancelacionStage(ErrorJob):
    """La etapa observo la cancelacion y se detuvo de forma cooperativa."""


@dataclass
class ContextoStage:
    """Lo unico que una etapa puede tocar del mundo exterior."""

    job_id: str
    attempt_id: str
    directorio: Path
    payload: Mapping[str, object] = field(default_factory=dict)
    checkpoint: Mapping[str, object] | None = None
    emitir: Callable[[MensajeWorker], None] = lambda _mensaje: None
    emitir_crudo: Callable[[bytes], None] = lambda _datos: None
    cancelado: Callable[[], bool] = lambda: False

    def ruta(self, relativa: str) -> Path:
        """Ruta de escritura dentro del staging del intento, y solo ahi.

        El aislamiento por proceso no sirve de nada si el propio contexto deja
        que una etapa componga `../..`: el worker escribiria fuera del proyecto
        mucho antes de que el coordinador llegue a validar manifiesto alguno.
        Se valida con la gramatica de los dos sistemas y se rechaza cualquier
        enlace intermedio antes de devolver la ruta.
        """
        destino = self.directorio
        for parte in exigir_ruta_interna(relativa, "ruta de la etapa"):
            destino = destino / parte
            if destino.is_symlink():
                raise ErrorJob(f"La etapa intento escribir a traves de un enlace: {relativa!r}")
        return destino

    def estado(self, nombre: str) -> Path:
        """Ruta de estado del job, compartida entre intentos y dentro de `cache/`."""
        carpeta = self.directorio.parent / DIRECTORIO_ESTADO
        carpeta.mkdir(parents=True, exist_ok=True)
        return carpeta / exigir_componente_interno(nombre, "nombre de estado de la etapa")

    def progreso(self, hechas: float, totales: float | None = None,
                 clave: str = "stage.progreso") -> None:
        self.emitir(MensajeWorker(tipo=TipoMensaje.PROGRESO, clave=clave,
                                  unidades_hechas=hechas, unidades_totales=totales))

    def publicar_checkpoint(self, datos: Mapping[str, object],
                            clave: str = "stage.checkpoint") -> None:
        self.emitir(MensajeWorker(tipo=TipoMensaje.CHECKPOINT, clave=clave, checkpoint=dict(datos)))

    def esperar(self, segundos: float) -> None:
        """Duerme en rodajas para que la cancelacion siga siendo cooperativa."""
        restante = segundos
        while restante > 0:
            if self.cancelado():
                raise CancelacionStage("cancelado durante la espera")
            time.sleep(min(RODAJA_ESPERA, restante))
            restante -= RODAJA_ESPERA


def _entero(payload: Mapping[str, object], clave: str, defecto: int | None = None) -> int | None:
    valor = payload.get(clave, defecto)
    return None if valor is None else int(valor)


def stage_prueba(contexto: ContextoStage) -> ManifiestoSalida | None:
    """Etapa determinista con fallos inyectables.

    Devolver `None` significa "ya emiti mi propio mensaje terminal": es como se
    reproducen un resultado sin manifiesto y una linea que rompe el protocolo,
    dos casos que no pueden expresarse devolviendo un manifiesto valido.
    """
    payload = contexto.payload
    pasos = _entero(payload, "pasos", 3) or 0
    retardo = float(payload.get("retardo", 0.0) or 0.0)
    crash_en = _entero(payload, "crash_en")
    error_en = _entero(payload, "error_en")
    codigo = str(payload.get("codigo", CODIGO_PERMANENTE_PRUEBA))
    checkpoint_cada = _entero(payload, "checkpoint_cada")
    respetar = bool(payload.get("respetar_cancelacion", True))
    nombre = str(payload.get("nombre_salida", "salida.txt"))
    semilla = str(payload.get("contenido", "ok"))

    inicio = int((contexto.checkpoint or {}).get("paso", 0))
    lineas = list((contexto.checkpoint or {}).get("lineas", []))

    # Fallo transitorio *que se cura*: el primer intento crea el testigo y falla;
    # el siguiente lo encuentra y sigue. Es determinista dado el sistema de
    # archivos, y sin el no habria forma de distinguir "reintenta" de "reintenta
    # y sigue fallando igual".
    testigo = payload.get("marcador_transitorio")
    if testigo:
        # Solo un *nombre*: la ruta la decide el contexto, que la confina bajo
        # `cache/jobs/<job>/_estado/`. Aceptar una ruta del payload permitiria a
        # la etapa escribir donde quisiera.
        marca = contexto.estado(str(testigo))
        if not marca.exists():
            marca.write_text("1", encoding="utf-8")
            raise FalloStage(str(payload.get("codigo_transitorio", CODIGO_TRANSITORIO_PRUEBA)),
                             "fallo transitorio inyectado en el primer intento")

    for paso in range(inicio, pasos):
        if respetar and contexto.cancelado():
            raise CancelacionStage("cancelado entre pasos")
        if crash_en is not None and paso == crash_en:
            # Muerte brutal: ni excepcion ni mensaje. Es el caso que el
            # coordinador debe sobrevivir sin ayuda del hijo.
            os._exit(9)
        if error_en is not None and paso == error_en:
            raise FalloStage(codigo, f"fallo inyectado en el paso {paso}")
        if retardo:
            if respetar:
                contexto.esperar(retardo)
            else:
                time.sleep(retardo)  # ignora la cancelacion: fuerza el timeout
        lineas.append(f"{semilla}:{paso}")
        contexto.progreso(paso + 1, pasos, clave="stage.prueba.paso")
        if checkpoint_cada and (paso + 1) % checkpoint_cada == 0:
            contexto.publicar_checkpoint({"paso": paso + 1, "lineas": lineas})

    invalida = payload.get("salida_invalida")
    if invalida == "protocolo":
        contexto.emitir_crudo(b"esto no es json ni pretende serlo\n")
        return None
    if invalida == "sin_manifiesto":
        contexto.emitir(MensajeWorker(tipo=TipoMensaje.RESULTADO))
        return None

    destino = contexto.ruta(nombre)
    destino.parent.mkdir(parents=True, exist_ok=True)
    contenido = ("\n".join(lineas) + "\n").encode("utf-8") if lineas else b""
    if invalida != "ausente":
        destino.write_bytes(contenido)

    if invalida == "escapada":
        declarados = (ArchivoSalida("../fuera.txt", len(contenido)),)
    elif invalida == "tamano":
        declarados = (ArchivoSalida(nombre, len(contenido) + 7),)
    else:
        declarados = (ArchivoSalida(nombre, len(contenido)),)
    return ManifiestoSalida(declarados, {"kind": "prueba", "reanudado_desde": inicio,
                                         "pasos": pasos})


DEFINICION_PRUEBA = StageDefinition(
    nombre=NOMBRE_STAGE_PRUEBA,
    version_contrato=VERSION_STAGE_PRUEBA,
    salidas=("salida.txt",),
    recursos=Recursos(cpu=1),
    soporta_checkpoint=True,
    politica_retry=PoliticaRetry(max_intentos=3,
                                 codigos_transitorios=frozenset({CODIGO_TRANSITORIO_PRUEBA})),
    politica_cancelacion=PoliticaCancelacion(timeout_cooperativo=1.0),
)

Etapa = Callable[[ContextoStage], "ManifiestoSalida | None"]

DEFINICION_PRUEBA_GPU = replace(DEFINICION_PRUEBA, nombre=NOMBRE_STAGE_PRUEBA_GPU,
                                recursos=Recursos(gpu=1))

REGISTRO_STAGES: dict[str, Etapa] = {NOMBRE_STAGE_PRUEBA: stage_prueba,
                                     NOMBRE_STAGE_PRUEBA_GPU: stage_prueba}
DEFINICIONES: dict[str, StageDefinition] = {NOMBRE_STAGE_PRUEBA: DEFINICION_PRUEBA,
                                            NOMBRE_STAGE_PRUEBA_GPU: DEFINICION_PRUEBA_GPU}


def registrar(definicion: StageDefinition, etapa: Etapa) -> None:
    REGISTRO_STAGES[definicion.nombre] = etapa
    DEFINICIONES[definicion.nombre] = definicion
