"""Errores tipados de persistencia sin detalles de adaptador."""


class ErrorProyecto(Exception):
    """No se pudo abrir o modificar un proyecto."""


class ErrorMigracionProyecto(ErrorProyecto):
    """Una migracion se revirtio y el proyecto anterior fue restaurado."""


class ErrorFuenteProyecto(ErrorProyecto):
    """Una fuente no pudo incorporarse o relocalizarse con seguridad."""
