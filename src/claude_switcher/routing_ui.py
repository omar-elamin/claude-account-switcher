"""Menu for choosing which Claude Code usage drives auto-switching.

Presentation only. The caller passes in the MenuItem factory and callback.
"""

_CHOICES = (
    ('fable', 'Fable usage'),
    ('weekly', 'Weekly usage'),
)


def routing_menu(menu_item, selected: str, callback):
    """Return a 'Route based on' submenu with the selected basis checked."""
    menu = menu_item('Route based on')
    menu.add(menu_item('Claude Code', callback=None))
    for basis, title in _CHOICES:
        item = menu_item(title, callback=callback)
        item._routing_basis = basis
        item.state = 1 if basis == selected else 0
        menu.add(item)
    return menu
