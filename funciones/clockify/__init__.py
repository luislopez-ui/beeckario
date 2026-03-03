"""Clockify integration package.

Este paquete contiene:
- main_clockify.py: orquestador que interpreta la solicitud del usuario y ejecuta la acción.
- flows/: acciones modularizadas (crear, modificar, eliminar).
- project_lookup.py: búsqueda de Project ID desde directorios/clockify_proyectos.xlsx.

La entidad principal que manejamos aquí es el "registro" (time entry) de Clockify.
"""

