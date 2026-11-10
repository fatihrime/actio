"""Actio's HTTP interface: routes, and what every route shares.

This is the only part of the application that knows the web framework. It turns
requests into calls on the modules below it (`actio.identity`, `actio.core`), and their
results and errors into responses.
"""
