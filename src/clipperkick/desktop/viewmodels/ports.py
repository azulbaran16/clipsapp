"""El puerto que mantiene vivo el bucle de eventos.

Abrir un proyecto migra esquemas, valida invariantes y recorre el filesystem:
en el hilo de la interfaz eso es una ventana congelada. El view model no puede
resolverlo por si mismo sin importar Qt, asi que declara lo que necesita —enviar
trabajo fuera y recibir el resultado *de vuelta en el hilo de la interfaz*— y
deja que la carcasa provea la implementacion real.

Que las continuaciones vuelvan al hilo de la interfaz es parte del contrato, no
un detalle del adaptador: si no lo fuese, cada view model tendria que protegerse
con locks contra su propia vista.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol


class Ejecutor(Protocol):
    def enviar(self, trabajo: Callable[[], Any],
               al_terminar: Callable[[Any], None],
               al_fallar: Callable[[BaseException], None]) -> None: ...


class EjecutorInmediato:
    """Ejecuta en el acto. Para pruebas y para arranques sin bucle de eventos.

    Conserva el contrato completo —incluida la captura de errores— para que una
    prueba que pasa aqui signifique algo sobre el comportamiento real.
    """

    def enviar(self, trabajo: Callable[[], Any],
               al_terminar: Callable[[Any], None],
               al_fallar: Callable[[BaseException], None]) -> None:
        try:
            resultado = trabajo()
        except Exception as error:
            al_fallar(error)
            return
        al_terminar(resultado)
