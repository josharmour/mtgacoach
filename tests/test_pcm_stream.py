from types import SimpleNamespace
from unittest.mock import Mock

from PySide6.QtMultimedia import QAudio

from arenamcp.desktop.pcm_stream import PcmStreamPlayer


def test_network_end_waits_for_audio_device_to_drain(qapp):
    player = PcmStreamPlayer()
    sink = Mock()
    sink.error.return_value = SimpleNamespace(value=QAudio.NoError.value)
    sink.bytesFree.return_value = 48
    sink.state.return_value = SimpleNamespace(value=QAudio.ActiveState.value)
    device = Mock(write=Mock(side_effect=len))
    player.sink, player.device = sink, device
    events = []
    player.started.connect(lambda: events.append("started"))
    player.finished.connect(lambda: events.append("finished"))
    player.append(b"\0\0" * 48)
    assert events == ["started"]
    player.finish()
    assert events == ["started"]
    sink.state.return_value = SimpleNamespace(value=QAudio.IdleState.value)
    player._pump()
    assert events == ["started", "finished"]
    assert sum(len(call.args[0]) for call in device.write.call_args_list) == 96
    sink.reset.assert_called_once()


def test_stop_discards_buffered_audio_without_reporting_success(qapp):
    player = PcmStreamPlayer()
    sink = Mock()
    sink.error.return_value = QAudio.NoError
    sink.bytesFree.return_value = 0
    player.sink, player.device = sink, Mock()
    finished = Mock()
    player.finished.connect(finished)
    player.append(b"\0\0" * 24)
    player.stop()
    player._pump()
    assert not player.pending
    finished.assert_not_called()
    sink.reset.assert_called_once()
