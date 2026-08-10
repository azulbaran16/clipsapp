"""Errores que forman parte del lenguaje de la aplicacion."""


class ErrorAmigable(Exception):
    """Error que puede mostrarse directamente a la persona usuaria."""


class ErrorSondeoMedia(ErrorAmigable):
    """No fue posible obtener los datos tecnicos de un archivo multimedia."""
