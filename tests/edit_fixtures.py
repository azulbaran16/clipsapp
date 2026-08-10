"""Fixtures pequenas compartidas por las pruebas de T06."""

from clipperkick.domain.edit.document import EditDocument
from clipperkick.domain.edit.timebase import Rango, Timebase
from clipperkick.domain.edit.tracks import (
    CaptionTrack, Cue, FramingTrack, KeyframeEncuadre, Palabra, ReferenciaPreset,
    SourceSegment, VentanaEncuadre,
)


def documento(*, texto: str = "hola mundo", document_id: str = "doc-1",
              duracion: int = 10_000) -> EditDocument:
    cue = Cue(
        id="cue-1", rango=Rango(1_000, min(4_000, duracion)), texto=texto,
        palabras=(
            Palabra("word-1", "hola", Rango(1_000, min(2_000, duracion))),
            Palabra("word-2", "mundo", Rango(min(2_000, duracion - 1),
                                               min(4_000, duracion))),
        ) if duracion >= 4_000 else (),
    )
    return EditDocument(
        document_id=document_id,
        source_id="source-1",
        timebase=Timebase(1_000),
        duracion=duracion,
        preset=ReferenciaPreset("gaming", "1"),
        source_segments=(SourceSegment("segment-1", Rango(50_000, 50_000 + duracion)),),
        framing_track=FramingTrack(keyframes=(
            KeyframeEncuadre("frame-1", 0, VentanaEncuadre(0, 0, 1_000, 1_000)),
        )),
        caption_track=CaptionTrack(cues=(cue,)),
    )
