"""Codec del protocolo estructurado entre coordinador y worker.

Una linea, un objeto JSON, un mensaje. El formato es deliberadamente aburrido
—NDJSON sobre `stdout`— porque el contrato que importa no es el transporte sino
que **nada** de lo que cruza la frontera sea texto para humanos: cada mensaje
lleva tipo, claves localizables y unidades, y el diagnostico libre viaja en un
campo aparte que ninguna decision consulta.

El mismo modulo lo usan padre e hijo: si el codec divergiera, el bug seria
invisible hasta produccion.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
import math

from clipperkick.domain.jobs import (
    PROTOCOLO_WORKER, ArchivoSalida, ErrorProtocoloWorker, ErrorValidacionSalida, ManifiestoSalida,
    MensajeWorker, TipoMensaje,
)


CANAL_CANCELACION = "cancelar"
LIMITE_LINEA = 1 << 20  # 1 MiB: un manifiesto legitimo nunca se acerca.


def _numero(valor: object, campo: str = "valor") -> float | None:
    """Numero finito y no booleano, o `None`.

    `True` es un entero para Python: sin excluirlo, `unidades_hechas: true` se
    convertiria en un progreso de 1.0 perfectamente creible. `NaN` e `inf` se
    rechazan porque ninguna comparacion posterior con ellos significa nada.
    """
    if valor is None:
        return None
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        raise ErrorProtocoloWorker(f"El worker envio un '{campo}' que no es un numero.")
    if not math.isfinite(valor):
        raise ErrorProtocoloWorker(f"El worker envio un '{campo}' no finito.")
    return float(valor)


def _entero(valor: object, campo: str) -> int | None:
    if valor is None:
        return None
    if isinstance(valor, bool) or not isinstance(valor, int):
        raise ErrorProtocoloWorker(f"El worker envio un '{campo}' que no es un entero.")
    return valor


def _texto(valor: object, campo: str, defecto: str | None = None) -> str | None:
    if valor is None:
        return defecto
    if not isinstance(valor, str):
        raise ErrorProtocoloWorker(f"El worker envio un '{campo}' que no es texto.")
    return valor


def _mapping(valor: object, contexto: str) -> Mapping[str, object]:
    if not isinstance(valor, dict):
        raise ErrorProtocoloWorker(f"El worker envio un '{contexto}' que no es un objeto.")
    return valor


def codificar_manifiesto(manifiesto: ManifiestoSalida) -> dict[str, object]:
    return {"archivos": [{"ruta": archivo.ruta, "bytes": archivo.bytes}
                         for archivo in manifiesto.archivos],
            "metadata": dict(manifiesto.metadata)}


def decodificar_manifiesto(datos: object) -> ManifiestoSalida:
    documento = _mapping(datos, "manifiesto")
    archivos = documento.get("archivos", [])
    if not isinstance(archivos, list):
        raise ErrorProtocoloWorker("El manifiesto no declara una lista de archivos.")
    declarados = []
    for archivo in archivos:
        documento_archivo = _mapping(archivo, "archivo")
        ruta = _texto(documento_archivo.get("ruta"), "ruta de archivo")
        tamano = _entero(documento_archivo.get("bytes"), "tamano de archivo")
        if ruta is None or tamano is None:
            raise ErrorProtocoloWorker("El manifiesto declara un archivo incompleto.")
        try:
            declarados.append(ArchivoSalida(ruta, tamano))
        except ErrorValidacionSalida as error:
            raise ErrorProtocoloWorker(f"El manifiesto declara un archivo invalido: {error}") from error
    declarados = tuple(declarados)
    return ManifiestoSalida(declarados, dict(_mapping(documento.get("metadata", {}), "metadata")))


def codificar(mensaje: MensajeWorker) -> bytes:
    """Serializa un mensaje como una linea NDJSON en UTF-8."""
    documento: dict[str, object] = {"protocolo": PROTOCOLO_WORKER, "tipo": mensaje.tipo.value}
    if mensaje.clave:
        documento["clave"] = mensaje.clave
    for nombre, valor in (("unidades_hechas", mensaje.unidades_hechas),
                          ("unidades_totales", mensaje.unidades_totales),
                          ("codigo", mensaje.codigo), ("detalle", mensaje.detalle),
                          ("pid", mensaje.pid), ("checkpoint", mensaje.checkpoint)):
        if valor is not None:
            documento[nombre] = valor
    if mensaje.manifiesto is not None:
        documento["manifiesto"] = codificar_manifiesto(mensaje.manifiesto)
    return json.dumps(documento, ensure_ascii=False).encode("utf-8") + b"\n"


def decodificar(linea: bytes | str) -> MensajeWorker:
    """Interpreta una linea del worker. Todo lo que no encaje es protocolo roto."""
    if isinstance(linea, bytes):
        if len(linea) > LIMITE_LINEA:
            raise ErrorProtocoloWorker("El worker envio una linea desmesurada.")
        try:
            linea = linea.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ErrorProtocoloWorker("El worker envio bytes que no son UTF-8.") from error
    try:
        documento = json.loads(linea)
    except ValueError as error:
        raise ErrorProtocoloWorker("El worker envio una linea que no es JSON.") from error
    documento = _mapping(documento, "mensaje")
    if documento.get("protocolo") != PROTOCOLO_WORKER:
        raise ErrorProtocoloWorker("El worker habla una version de protocolo distinta.")
    try:
        tipo = TipoMensaje(documento.get("tipo"))
    except ValueError as error:
        raise ErrorProtocoloWorker("El worker envio un tipo de mensaje desconocido.") from error
    checkpoint = documento.get("checkpoint")
    manifiesto = documento.get("manifiesto")
    return MensajeWorker(
        tipo=tipo,
        clave=_texto(documento.get("clave"), "clave", "") or "",
        unidades_hechas=_numero(documento.get("unidades_hechas"), "unidades_hechas"),
        unidades_totales=_numero(documento.get("unidades_totales"), "unidades_totales"),
        checkpoint=None if checkpoint is None else dict(_mapping(checkpoint, "checkpoint")),
        manifiesto=None if manifiesto is None else decodificar_manifiesto(manifiesto),
        codigo=_texto(documento.get("codigo"), "codigo"),
        detalle=_texto(documento.get("detalle"), "detalle"),
        pid=_entero(documento.get("pid"), "pid"),
    )


def codificar_control(tipo: str) -> bytes:
    return json.dumps({"protocolo": PROTOCOLO_WORKER, "tipo": tipo}).encode("utf-8") + b"\n"


def es_control_de_cancelacion(linea: bytes) -> bool:
    """El canal de control es de un solo bit; cualquier ruido se ignora.

    Un worker no debe morir porque el padre le mando algo raro: la unica orden
    que existe es cancelar, y lo que no lo sea no cambia nada.
    """
    try:
        documento = json.loads(linea.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return False
    return (isinstance(documento, dict)
            and documento.get("protocolo") == PROTOCOLO_WORKER
            and documento.get("tipo") == CANAL_CANCELACION)
