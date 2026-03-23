"""Detect player actions from Arena log messages.

Parses response messages to determine what the player actually did,
allowing comparison with advisor suggestions.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class ActionDetector:
    """Detects and describes player actions from Arena message responses."""
    
    def __init__(self, card_db=None):
        """Initialize action detector.
        
        Args:
            card_db: Optional CardDatabase for enriching card names
        """
        self.card_db = card_db
    
    def detect_action(self, response_msg: dict, game_state: dict) -> Optional[str]:
        """Detect what action the player took from a response message.
        
        Args:
            response_msg: Arena response message (e.g., SelectNResp, SelectTargetsResp)
            game_state: Current game state for context
            
        Returns:
            Human-readable description of the action, or None if unknown
        """
        msg_type = response_msg.get("type") or response_msg.get("clientToGreMessage", {}).get("type")
        
        if not msg_type:
            return None
        
        # Dispatch to specific handler
        if msg_type == "ClientToGreMessage_SelectNResp":
            return self._handle_select_n(response_msg, game_state)
        elif msg_type == "ClientToGreMessage_SelectTargetsResp":
            return self._handle_select_targets(response_msg, game_state)
        elif msg_type == "ClientToGreMessage_GroupOptionResp":
            return self._handle_modal_choice(response_msg, game_state)
        elif msg_type == "ClientToGreMessage_DeclareAttackersResp":
            return self._handle_declare_attackers(response_msg, game_state)
        elif msg_type == "ClientToGreMessage_DeclareBlockersResp":
            return self._handle_declare_blockers(response_msg, game_state)
        elif msg_type == "ClientToGreMessage_SubmitDeckResp":
            return self._handle_mulligan(response_msg, game_state)
        
        return None
    
    def _handle_select_n(self, msg: dict, game_state: dict) -> Optional[str]:
        """Handle SelectN response (discard, scry, etc.)."""
        resp = msg.get("selectNResp", {})
        selected_ids = resp.get("selectedObjectInstanceIds", [])
        
        if not selected_ids:
            return "Passed (selected nothing)"
        
        # Look up card names
        card_names = []
        for obj_id in selected_ids:
            card_name = self._get_card_name_by_instance(obj_id, game_state)
            if card_name:
                card_names.append(card_name)
        
        if not card_names:
            return f"Selected {len(selected_ids)} card(s)"
        
        # Infer action type from context
        # This is heuristic - could be improved with request context
        if len(card_names) == 1:
            return f"Selected: {card_names[0]}"
        else:
            return f"Selected: {', '.join(card_names)}"
    
    def _handle_select_targets(self, msg: dict, game_state: dict) -> Optional[str]:
        """Handle target selection response."""
        resp = msg.get("selectTargetsResp", {})
        targets = resp.get("targets", [])
        
        if not targets:
            return "Canceled (no targets)"
        
        target_ids = [t.get("targetId") for t in targets if t.get("targetId")]
        
        # Look up target names
        target_names = []
        for target_id in target_ids:
            name = self._get_card_name_by_instance(target_id, game_state)
            if name:
                target_names.append(name)
        
        if not target_names:
            return f"Targeted {len(target_ids)} object(s)"
        
        return f"Targeted: {', '.join(target_names)}"
    
    def _handle_modal_choice(self, msg: dict, game_state: dict) -> Optional[str]:
        """Handle modal spell choice response."""
        resp = msg.get("groupOptionResp", {})
        selected_option = resp.get("selectedOption")
        
        if selected_option is None:
            return "Canceled modal choice"
        
        return f"Chose mode {selected_option}"
    
    def _handle_declare_attackers(self, msg: dict, game_state: dict) -> Optional[str]:
        """Handle attacker declaration response."""
        resp = msg.get("declareAttackersResp", {})
        attackers = resp.get("attackers", [])
        
        if not attackers:
            return "No attackers declared"
        
        attacker_ids = [a.get("attackerId") for a in attackers]
        attacker_names = []
        
        for att_id in attacker_ids:
            name = self._get_card_name_by_instance(att_id, game_state)
            if name:
                attacker_names.append(name)
        
        if not attacker_names:
            return f"Attacked with {len(attacker_ids)} creature(s)"
        
        return f"Attacked with: {', '.join(attacker_names)}"
    
    def _handle_declare_blockers(self, msg: dict, game_state: dict) -> Optional[str]:
        """Handle blocker declaration response."""
        resp = msg.get("declareBlockersResp", {})
        blockers = resp.get("blockers", [])
        
        if not blockers:
            return "No blockers declared"
        
        blocker_names = []
        for blocker in blockers:
            blocker_id = blocker.get("blockerId")
            name = self._get_card_name_by_instance(blocker_id, game_state)
            if name:
                blocker_names.append(name)
        
        if not blocker_names:
            return f"Blocked with {len(blockers)} creature(s)"
        
        return f"Blocked with: {', '.join(blocker_names)}"
    
    def _handle_mulligan(self, msg: dict, game_state: dict) -> Optional[str]:
        """Handle mulligan decision response."""
        # In Arena, mulligan choice is implicit from deck size
        # If you kept, deck stays same size
        # If you mulled, you drew fewer cards
        # This is a simplified heuristic
        
        # We can't easily determine from the response alone
        # Would need to compare hand size before/after
        return "Mulligan decision made"
    
    def _get_card_name_by_instance(self, instance_id: int, game_state: dict) -> Optional[str]:
        """Look up card name by instance ID from game state.
        
        Args:
            instance_id: Object instance ID
            game_state: Current game state snapshot
            
        Returns:
            Card name if found
        """
        # Search all zones
        for zone_name in ["battlefield", "hand", "graveyard", "stack", "exile"]:
            zone = game_state.get(zone_name, [])
            for obj in zone:
                if obj.get("instance_id") == instance_id:
                    return obj.get("name", "Unknown")
        
        # Not found
        return None


# Global instance
_detector: Optional[ActionDetector] = None


def get_detector() -> ActionDetector:
    """Get global action detector instance."""
    global _detector
    if _detector is None:
        _detector = ActionDetector()
    return _detector


def detect_player_action(response_msg: dict, game_state: dict) -> Optional[str]:
    """Detect what the player did from a response message."""
    return get_detector().detect_action(response_msg, game_state)
