import os
from unittest.mock import MagicMock, patch

import pytest
try:
    from PySide6.QtGui import QKeySequence
except ImportError:
    pytest.skip("PySide6 C-extensions not available", allow_module_level=True)

from arenamcp.desktop.hotkeys import HotkeyManager

def test_hotkey_manager_register(qtbot):
    parent = MagicMock()
    manager = HotkeyManager(parent=parent)

    callback = MagicMock()
    
    with patch("os.name", "nt"):
        with patch("keyboard.add_hotkey") as mock_add:
            manager.register("F3", callback)
            mock_add.assert_called_once_with("F3", callback)
            
    with patch("os.name", "posix"):
        manager.register("F4", callback)
        # Verify shortcut was created internally for posix
        assert "F4" in manager._shortcuts

def test_hotkey_manager_unregister(qtbot):
    parent = MagicMock()
    manager = HotkeyManager(parent=parent)
    
    callback = MagicMock()
    
    with patch("os.name", "nt"):
        with patch("keyboard.unhook_all") as mock_unhook:
            manager.unregister_all()
            mock_unhook.assert_called_once()
            
    with patch("os.name", "posix"):
        manager.register("F5", callback)
        assert "F5" in manager._shortcuts
        manager.unregister_all()
        assert not manager._shortcuts
