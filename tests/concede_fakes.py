"""A ConcedeController harness: scripted state, recorded advice/announcements/concedes, manual clock."""

from __future__ import annotations

from typing import Any

from arenamcp.concede import ConcedeController


class Harness:
    """A ConcedeController on ``state`` with every side effect recorded and a manual clock."""

    def __init__(
        self,
        state: dict,
        *,
        autopilot: bool = True,
        bridge: bool = True,
        result: Any = None,
        ends_on_concede: bool = True,
    ):
        self.state = state
        self.autopilot = autopilot
        self.bridge = bridge
        self.game_over = False
        self.ends_on_concede = ends_on_concede
        self.settings: dict[str, Any] = {}
        self.result = {"ok": True} if result is None else result
        self.advice: list[str] = []
        self.announced: list[str] = []
        self.events: list[dict] = []
        self.concedes: list[dict] = []
        self.threads: list = []
        self.waits: list[float] = []
        self.on_concede = None
        self.on_announce = None
        self.now = 0.0
        self.controller = ConcedeController(
            get_state=lambda: self.state,
            concede_fn=self._concede,
            advise=lambda text, state=None: self.advice.append(text),
            emit=self._emit,
            settings_get=lambda key, default=None: self.settings.get(key, default),
            autopilot_on=lambda: self.autopilot,
            bridge_ready=lambda: self.bridge,
            game_over=lambda: self.game_over,
            announce=self._announce,
            start_thread=lambda target, armed: self.threads.append((target, armed)),
            wait=self._wait,
            clock=lambda: self.now,
        )
        self.on_emit = None

    def _emit(self, payload: dict) -> None:
        self.events.append(payload)
        if self.on_emit:
            self.on_emit(payload)

    def _announce(self, text: str, state: Any, cancelled: Any) -> bool:
        # The countdown must not have started while the offer is being heard.
        assert "armed" not in self.states
        self.announced.append(text)
        if self.on_announce:
            self.on_announce()
        return not cancelled()

    def _wait(self, event: Any, seconds: float) -> bool:
        self.waits.append(seconds)
        return event.is_set()

    def _concede(self, state: dict) -> Any:
        self.concedes.append(state)
        if self.on_concede:
            self.on_concede()
        if self.ends_on_concede and self.result.get("ok"):
            self.game_over = True
        return self.result

    def observe(self, key: Any = ("fra", 1), times: int = 2) -> None:
        """Observe like the coaching loop: the verdict must hold on two polls."""
        for _ in range(times):
            self.now += 1.0
            self.controller.observe(self.state, key)

    def run_countdown(self) -> None:
        target, armed = self.threads.pop(0)
        target(armed)

    @property
    def states(self) -> list[str]:
        return [event["state"] for event in self.events]
