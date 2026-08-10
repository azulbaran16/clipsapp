"""Version, forma canonica y migracion del documento de edicion.

Aqui viven las tres cosas que hacen que una revision guardada hoy siga siendo
legible manana:

1. **La forma canonica.** Un documento se serializa siempre con las mismas
   claves ordenadas y sin espacios sobrantes, de modo que dos revisiones
   equivalentes producen *los mismos bytes*. No es cosmetica: la igualdad de
   texto es lo que permite decidir si un patch cambio algo y si una cache de
   proxy sigue siendo valida sin volver a interpretar el documento.
2. **La version.** `schema_version` viaja dentro del documento, no fuera. Un
   documento arrancado de su contexto —copiado a un informe, adjuntado a un
   diagnostico— sigue diciendo como leerlo.
3. **La migracion.** `migrar_documento` sube cualquier version soportada hasta
   la actual. Es el unico lugar del sistema que sabe como era el formato
   anterior; el resto del dominio solo conoce `VERSION_DOCUMENTO`.

## Que cambio entre versiones

v1 media el tiempo en **segundos de coma flotante** y no tenia forma de decir
que un campo lo habia corregido una persona. v2 mide en ticks enteros sobre una
`Timebase` declarada y guarda marcas `manual` por campo.

La migracion cuantiza los segundos al tick mas cercano y **marca como manual
todo lo marcable**. Esto ultimo es deliberado y asimetrico a proposito: un
documento v1 no distingue lo que propuso el analisis de lo que corrigio la
persona. Si la migracion asumiese "todo automatico", el primer rerun de analisis
borraria en silencio correcciones humanas —un dano que nadie puede deshacer—;
asumiendo "todo manual" lo peor que pasa es que un rerun no proponga nada nuevo,
y de ahi se sale con `Restaurar automatico`, que es una accion explicita y
reversible.

v3 liga el snapshot persistido con su `revision_id` y da identidad estable a
cada palabra. La migracion v2 -> v3 deja `revision_id=None` porque un documento
aislado no permite inventarlo; el repositorio lo completa con la identidad de
la fila `EditRevision`. Los ids de palabra se derivan de cue + orden, que eran
estables en la forma canonica v2.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json

from .document import VERSION_DOCUMENTO, EditDocument
from .errors import ErrorEsquemaEdicion
from .timebase import TIMEBASE_PREDETERMINADO, Timebase
from .tracks import (
    AudioTrack, CapaAudio, CaptionTrack, Cue, EffectTrack, EventoEfecto, FramingTrack,
    KeyframeEncuadre, OverlayTrack, Palabra, RenderIntent, SourceSegment, Superposicion,
)


#: Versiones que esta build sabe leer. La actual y, como minimo, la
#: inmediatamente anterior: un proyecto guardado con la build previa tiene que
#: abrirse sin que nadie exporte y reimporte nada.
VERSIONES_SOPORTADAS = (1, 2, VERSION_DOCUMENTO)

#: Timebase con la que se cuantiza un documento v1, que no declaraba ninguna.
TIMEBASE_MIGRACION = TIMEBASE_PREDETERMINADO


def texto_canonico(datos: Mapping[str, object]) -> str:
    """Bytes estables para el mismo contenido, en cualquier maquina y version.

    `sort_keys` fija el orden, `separators` elimina el espacio decorativo y
    `ensure_ascii=False` conserva el texto real de los subtitulos en vez de
    convertirlo en secuencias de escape que dependerian del intercalador.
    """
    return json.dumps(datos, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def serializar(documento: EditDocument) -> str:
    return texto_canonico(documento.como_documento())


def deserializar(texto: str) -> EditDocument:
    """Texto guardado -> documento vigente, migrando si hace falta."""
    if not isinstance(texto, str):
        raise ErrorEsquemaEdicion("Un documento guardado tiene que ser texto.")
    try:
        datos = json.loads(texto)
    except ValueError as error:
        raise ErrorEsquemaEdicion("El documento guardado no es JSON legible.") from error
    return leer_documento(datos)


def leer_documento(datos: object) -> EditDocument:
    return EditDocument.desde_documento(migrar_documento(datos))


# --------------------------------------------------------------------------- #
# Migracion
# --------------------------------------------------------------------------- #

def version_de(datos: object) -> int:
    if not isinstance(datos, Mapping):
        raise ErrorEsquemaEdicion("El documento guardado no es un objeto.")
    version = datos.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ErrorEsquemaEdicion("El documento guardado no declara su version de esquema.")
    if version > VERSION_DOCUMENTO:
        raise ErrorEsquemaEdicion(
            f"El documento fue escrito con la version {version} y esta build entiende hasta"
            f" {VERSION_DOCUMENTO}; actualiza ClipsApp para abrirlo.")
    if version not in VERSIONES_SOPORTADAS:
        soportadas = ", ".join(str(numero) for numero in VERSIONES_SOPORTADAS)
        raise ErrorEsquemaEdicion(
            f"El documento declara la version {version} y esta build solo migra desde: {soportadas}.")
    return version


def migrar_documento(datos: object) -> dict[str, object]:
    """Sube el documento a `VERSION_DOCUMENTO`. Idempotente sobre la version actual."""
    version = version_de(datos)
    documento = dict(datos)  # type: ignore[arg-type]
    if version == 1:
        documento = _de_v1_a_v2(documento)
        version = 2
    if version == 2:
        documento = _de_v2_a_v3(documento)
    return documento


def _lista(datos: object, campo: str) -> list:
    if datos is None:
        return []
    if isinstance(datos, (str, bytes)) or not isinstance(datos, Sequence):
        raise ErrorEsquemaEdicion(f"El campo '{campo}' del documento v1 tiene que ser una lista.")
    return list(datos)


def _objeto(datos: object, campo: str) -> dict[str, object]:
    if datos is None:
        return {}
    if not isinstance(datos, Mapping):
        raise ErrorEsquemaEdicion(f"El campo '{campo}' del documento v1 tiene que ser un objeto.")
    return dict(datos)


def _marcas(tipo: type) -> list[str]:
    return sorted(getattr(tipo, "CAMPOS_MARCABLES", frozenset()))


def _rango(datos: Mapping[str, object], timebase: Timebase, campo: str) -> dict[str, object]:
    return {"inicio": timebase.cuantizar(datos.get("inicio_segundos"), f"{campo}.inicio_segundos"),
            "fin": timebase.cuantizar(datos.get("fin_segundos"), f"{campo}.fin_segundos")}


def _de_v1_a_v2(v1: Mapping[str, object]) -> dict[str, object]:
    """v1 (segundos, sin overrides) -> v2 (ticks enteros, todo marcado manual)."""
    timebase = Timebase(TIMEBASE_MIGRACION)
    segmentos = []
    for indice, fila in enumerate(_lista(v1.get("source_segments"), "source_segments")):
        entrada = _objeto(fila, f"source_segments[{indice}]")
        segmentos.append({
            "id": entrada.get("id"),
            "rango": _rango(entrada, timebase, f"source_segments[{indice}]"),
            "transicion_entrada": entrada.get("transicion_entrada", "corte"),
            "manual": _marcas(SourceSegment),
        })
    if not segmentos:
        raise ErrorEsquemaEdicion("El documento v1 no declara segmentos de la fuente.")

    encuadre_v1 = _objeto(v1.get("framing_track"), "framing_track")
    keyframes = []
    for indice, fila in enumerate(_lista(encuadre_v1.get("keyframes"), "framing_track.keyframes")):
        entrada = _objeto(fila, f"framing_track.keyframes[{indice}]")
        keyframes.append({
            "id": entrada.get("id"),
            "tick": timebase.cuantizar(entrada.get("segundo"),
                                       f"framing_track.keyframes[{indice}].segundo"),
            "ventana": _objeto(entrada.get("ventana"), f"framing_track.keyframes[{indice}].ventana"),
            "modo": entrada.get("modo", "gameplay"),
            "manual": _marcas(KeyframeEncuadre),
        })

    captions_v1 = _objeto(v1.get("caption_track"), "caption_track")
    cues = []
    for indice, fila in enumerate(_lista(captions_v1.get("cues"), "caption_track.cues")):
        entrada = _objeto(fila, f"caption_track.cues[{indice}]")
        palabras = []
        for orden, cruda in enumerate(_lista(entrada.get("palabras"),
                                             f"caption_track.cues[{indice}].palabras")):
            palabra = _objeto(cruda, f"caption_track.cues[{indice}].palabras[{orden}]")
            palabras.append({
                "texto": palabra.get("texto"),
                "rango": _rango(palabra, timebase, f"caption_track.cues[{indice}].palabras[{orden}]"),
                "confianza": palabra.get("confianza", 1000),
                "manual": _marcas(Palabra),
            })
        cues.append({
            "id": entrada.get("id"),
            "rango": _rango(entrada, timebase, f"caption_track.cues[{indice}]"),
            "texto": entrada.get("texto"), "palabras": palabras,
            "oculto": entrada.get("oculto", False), "manual": _marcas(Cue),
        })

    audio_v1 = _objeto(v1.get("audio_track"), "audio_track")
    capas = []
    for indice, fila in enumerate(_lista(audio_v1.get("capas"), "audio_track.capas")):
        entrada = _objeto(fila, f"audio_track.capas[{indice}]")
        envolvente = []
        for orden, cruda in enumerate(_lista(entrada.get("envolvente"),
                                             f"audio_track.capas[{indice}].envolvente")):
            punto = _objeto(cruda, f"audio_track.capas[{indice}].envolvente[{orden}]")
            envolvente.append({
                "tick": timebase.cuantizar(punto.get("segundo"),
                                           f"audio_track.capas[{indice}].envolvente[{orden}].segundo"),
                "ganancia_ddb": punto.get("ganancia_ddb", 0),
            })
        capas.append({
            "capa": entrada.get("capa"), "ganancia_ddb": entrada.get("ganancia_ddb", 0),
            "silenciada": entrada.get("silenciada", False), "envolvente": envolvente,
            "asset": entrada.get("asset"), "manual": _marcas(CapaAudio),
        })

    efectos_v1 = _objeto(v1.get("effect_track"), "effect_track")
    eventos = []
    for indice, fila in enumerate(_lista(efectos_v1.get("eventos"), "effect_track.eventos")):
        entrada = _objeto(fila, f"effect_track.eventos[{indice}]")
        eventos.append({
            "id": entrada.get("id"), "tipo": entrada.get("tipo"),
            "rango": _rango(entrada, timebase, f"effect_track.eventos[{indice}]"),
            "parametros": _objeto(entrada.get("parametros"),
                                  f"effect_track.eventos[{indice}].parametros"),
            "activo": entrada.get("activo", True), "manual": _marcas(EventoEfecto),
        })

    superposiciones_v1 = _objeto(v1.get("overlay_track"), "overlay_track")
    elementos = []
    for indice, fila in enumerate(_lista(superposiciones_v1.get("elementos"),
                                         "overlay_track.elementos")):
        entrada = _objeto(fila, f"overlay_track.elementos[{indice}]")
        elementos.append({
            "id": entrada.get("id"), "asset": entrada.get("asset"),
            "rango": _rango(entrada, timebase, f"overlay_track.elementos[{indice}]"),
            "anclaje": entrada.get("anclaje", "inferior_derecha"),
            "escala_milesimas": entrada.get("escala_milesimas", 150),
            "opacidad": entrada.get("opacidad", 100), "manual": _marcas(Superposicion),
        })

    render_v1 = _objeto(v1.get("render_intent"), "render_intent")
    render = dict(render_v1)
    render["manual"] = _marcas(RenderIntent)
    if "fps_logico" not in render:
        render["fps_logico"] = _fps_v1(v1.get("fps"))

    captions = {
        "cues": cues, "estilo": captions_v1.get("estilo", "limpio"),
        "tamano_relativo": captions_v1.get("tamano_relativo", 100),
        "desfase": _desfase_v1(captions_v1.get("desfase_segundos"), timebase),
        "manual": _marcas(CaptionTrack),
    }
    return {
        "schema_version": 2,
        "document_id": v1.get("document_id"),
        "source_id": v1.get("source_id"),
        "timebase": timebase.como_documento(),
        # La duracion se **deriva** de los segmentos ya cuantizados en vez de
        # cuantizar la que v1 declaraba: cuantizar por separado dos numeros que
        # tenian que sumar lo mismo produce, con la mitad de las entradas, un
        # documento que se rechaza a si mismo por un tick.
        "duracion": sum(segmento["rango"]["fin"] - segmento["rango"]["inicio"]
                        for segmento in segmentos),
        "intensidad": v1.get("intensidad", 50),
        "preset": _objeto(v1.get("preset"), "preset"),
        "source_segments": segmentos,
        "framing_track": {"aspecto": encuadre_v1.get("aspecto", "9:16"), "keyframes": keyframes,
                          "manual": _marcas(FramingTrack)},
        "caption_track": captions,
        "audio_track": {"capas": capas, "ducking_ddb": audio_v1.get("ducking_ddb", -90),
                        "ducking_activo": audio_v1.get("ducking_activo", True),
                        "manual": _marcas(AudioTrack)},
        "effect_track": {"eventos": eventos, "manual": _marcas(EffectTrack)},
        "overlay_track": {"elementos": elementos, "manual": _marcas(OverlayTrack)},
        "render_intent": render,
    }


def _de_v2_a_v3(v2: Mapping[str, object]) -> dict[str, object]:
    """v2 -> v3: identidad de revision y de palabras, sin perder ticks."""
    # Copia profunda de datos JSON: migrar nunca modifica el objeto entregado
    # por el caller ni deja listas compartidas entre versiones.
    documento = json.loads(json.dumps(v2, ensure_ascii=False))
    documento["schema_version"] = 3
    documento.setdefault("revision_id", None)
    caption_track = documento.get("caption_track")
    if isinstance(caption_track, dict):
        cues = caption_track.get("cues", [])
        if isinstance(cues, list):
            for cue_index, cue in enumerate(cues):
                if not isinstance(cue, dict):
                    continue
                cue_id = cue.get("id", f"cue-{cue_index}")
                words = cue.get("palabras", [])
                if not isinstance(words, list):
                    continue
                for word_index, word in enumerate(words):
                    if isinstance(word, dict):
                        word.setdefault("id", f"{cue_id}.palabra.{word_index}")
    return documento


def _fps_v1(valor: object) -> dict[str, object]:
    if valor is None:
        return {"numerador": 30, "denominador": 1}
    if isinstance(valor, Mapping):
        return dict(valor)
    if isinstance(valor, bool) or not isinstance(valor, int):
        raise ErrorEsquemaEdicion("El documento v1 declara un fps que no es entero.")
    return {"numerador": valor, "denominador": 1}


def _desfase_v1(valor: object, timebase: Timebase) -> int:
    """El desfase es el unico tiempo v1 que podia ser negativo."""
    if valor is None:
        return 0
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        raise ErrorEsquemaEdicion("El documento v1 declara un desfase que no es un numero.")
    signo = -1 if valor < 0 else 1
    return signo * timebase.cuantizar(abs(valor), "caption_track.desfase_segundos")


__all__ = [
    "TIMEBASE_MIGRACION", "VERSIONES_SOPORTADAS", "deserializar", "leer_documento",
    "migrar_documento", "serializar", "texto_canonico", "version_de",
]
