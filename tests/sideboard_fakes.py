"""Interprets the sideboard reflect batch, including its real clone and submit boundary."""

from collections import Counter

from tests.mac_world import Obj, World


class SideboardWorld(World):
    def __init__(self):
        super().__init__()
        self.next_handle = 100
        self.sent = []
        self.submitted = []
        self.submitted_handles = []
        self.before_submit = None
        self.lose_reply = False
        self.reject_at = None
        self.after_submit_failure = False
        self.retain_values = True
        self.new_scene_state = None
        self.main = [1] * 23 + [7001] * 17
        self.side = [2] * 3
        self.original = self.deck(self.main, self.side)
        self.original_message = self.add(2, "GREToClientMessage", GameStateId=40, MsgId=7)
        self.delegate = self.add(3, "Action`1")
        self.request = self.add(
            4,
            "SubmitDeckRequest",
            OriginalMessage=Obj(2),
            OnSubmit=Obj(3),
            Type="SubmitDeck",
            Deck=Obj(self.original),
        )
        self.constraints = self.add(
            5,
            "DeckConstraintInfo",
            MinDeckSize=40,
            MaxDeckSize=250,
            MinSideboardSize=0,
            MaxSideboardSize=250,
            MinCommanderSize=0,
            MaxCommanderSize=0,
        )
        self.info = self.add(
            6, "GameInfo", MatchID="match-318", GameNumber=1, SuperFormat="Limited", DeckConstraintInfo=Obj(5)
        )
        self.add(7, "MtgGameState", GameInfo=Obj(6))
        self.add(8, "Provider", LatestGameState=Obj(7))
        self.add(9, "Timer", RemainingTime=60.0)
        self.add(10, "DeckBuilderContext", IsSideboarding=True, IsLimited=True, IsReadOnly=False)
        self.editor_main = self.collection(
            [Obj(self.alloc("CardInDeck", Id=g, Quantity=n)) for g, n in Counter(self.main).items()]
        )
        self.editor = self.add(
            11,
            "DeckInfo",
            mainDeck=Obj(self.editor_main),
            commandZone=Obj(self.collection([])),
            companion=None,
        )
        self.model = self.add(12, "DeckBuilderModel", HasLoadedDeck=True)
        self.add(13, "ModelProvider", _model=Obj(12))
        self.add(14, "DeckBuilderWidget", Context=Obj(10), _isActive=True, ModelProvider=Obj(13))
        self.add(15, "SideboardInterface", _deckBuilder=Obj(14))
        self.scene_value = self.alloc(
            "DuelSceneState",
            MatchScene_SubScene="DuelScene",
            MatchScene_SubSceneTransitioning=False,
            MatchScene_LockSceneTransitions=True,
            FirstGameStatePlaybackComplete=True,
            GameComplete=True,
        )
        self.scene_observable = self.alloc("ObservableValue`1", Value=Obj(self.scene_value))
        self.add(16, "DuelSceneStateManager", CurrentState=Obj(self.scene_observable))
        self.styles = self.collection(["style-a"])
        self.add(17, "PlayerInfo", CardStyles=Obj(self.styles))
        self.add(18, "MatchManager", LocalPlayerInfo=Obj(17))
        self.add(
            19,
            "SubmitDeckWorkflow",
            BaseRequest=Obj(4),
            _gameStateProvider=Obj(8),
            _sideboardTimer=Obj(9),
            _sideboardInterface=Obj(15),
            _duelSceneStateController=Obj(16),
            _matchManager=Obj(18),
        )
        self.add(20, "WorkflowController", CurrentWorkflow=Obj(19))
        self.finds["GameManager"] = self.add(21, "GameManager", WorkflowController=Obj(20))
        # A separate saved deck must never be a mutation target.
        self.saved = self.deck(self.main, self.side)
        self.local_deck = None

    def alloc(self, cls, **fields):
        self.next_handle += 1
        return self.add(self.next_handle, cls, **fields)

    def collection(self, entries):
        return self.alloc("RepeatedField`1", _items=list(entries), Count=len(entries))

    def deck(self, main, side):
        return self.alloc(
            "DeckMessage",
            DeckCards=Obj(self.collection(main)),
            SideboardCards=Obj(self.collection(side)),
            CommanderCards=Obj(self.collection([])),
            DeckMessageFieldFour=0,
        )

    def node(self, value):
        if not isinstance(value, Obj):
            return value
        fields = self.objects[value.handle]
        node = {"$h": value.handle, "$c": self.classes[value.handle]}
        if "_items" in fields:
            node.update({"$n": len(fields["_items"]), "$items": [self.node(x) for x in fields["_items"]]})
        else:
            node.update({k: v for k, v in fields.items() if not isinstance(v, Obj)})
        return node

    def ids(self, deck, pile):
        return list(self.objects[self.objects[deck][pile].handle]["_items"])

    def send(self, command, timeout):
        ops = command["ops"]
        self.sent.append(ops)
        mutation = any(op.get("method") == "SubmitDeck" for op in ops)
        if mutation and self.before_submit:
            self.before_submit(self)
        result = []
        for i, op in enumerate(ops):
            try:
                kind = op["op"]
                if kind == "pending":
                    value = Obj(self.request)
                elif kind == "find":
                    value = Obj(self.finds[op["class"]])
                elif kind == "new":
                    assert op["class"] == "Wotc.Mtga.DuelScene.DuelSceneState"
                    args = op["args"]
                    kept = [self.objects[self.target(args[i], result)]["scalar"] for i in (0, 1, 3, 4)]
                    value = Obj(
                        self.alloc(
                            "DuelSceneState",
                            MatchScene_SubScene=kept[0],
                            MatchScene_SubSceneTransitioning=kept[1],
                            MatchScene_LockSceneTransitions=args[2]["bool"],
                            FirstGameStatePlaybackComplete=kept[2],
                            GameComplete=kept[3],
                        )
                    )
                else:
                    h = self.target(op["target"], result)
                    if h is None:
                        if op.get("optional"):
                            result.append(None)
                            continue
                        raise ValueError("null target")
                    fields = self.objects[h]
                    if kind == "expect_pending":
                        if h != self.request:
                            raise ValueError("pending request changed")
                        value = True
                    elif kind == "expect":
                        if op.get("class") and self.classes[h] != op["class"]:
                            raise ValueError("class changed")
                        if "equals" in op and fields.get(op["member"]) != op["equals"]:
                            raise ValueError("changed " + op["member"])
                        value = True
                    elif kind == "get":
                        value = fields.get(op["member"])
                        if op.get("retain_value") and not isinstance(value, Obj) and self.retain_values:
                            value = Obj(self.alloc("BoxedValue", scalar=value))
                    elif kind == "call":
                        method = op["method"]
                        if self.reject_at == method:
                            raise ValueError("scripted pre-submit rejection")
                        args = op["args"]
                        self.calls.append((h, method, args))
                        value = None
                        if method == "GetServerModel":
                            value = Obj(self.editor)
                        elif method == "get_Item":
                            value = fields["_items"][args[0]["int"]]
                        elif method == "Clone":
                            assert h == self.original
                            copy = self.deck(self.ids(h, "DeckCards"), self.ids(h, "SideboardCards"))
                            self.objects[copy]["CommanderCards"] = Obj(
                                self.collection(self.ids(h, "CommanderCards"))
                            )
                            self.objects[copy]["DeckMessageFieldFour"] = fields["DeckMessageFieldFour"]
                            value = Obj(copy)
                        elif method == "Clear":
                            fields["_items"].clear()
                            fields["Count"] = 0
                        elif method == "Add":
                            fields["_items"].append(args[0]["uint"])
                            fields["Count"] += 1
                        elif method == "SubmitDeck":
                            assert h == self.request and fields["OnSubmit"] is not None
                            deck = self.target(args[0], result)
                            self.submitted_handles.append(deck)
                            self.submitted.append(
                                (self.ids(deck, "DeckCards"), self.ids(deck, "SideboardCards"))
                            )
                            fields["OnSubmit"] = None
                            if self.lose_reply:
                                raise TimeoutError("reply lost after accepted submission")
                        elif method == "ToArray":
                            value = Obj(self.collection(fields["_items"]))
                        elif method == "SetState":
                            if self.after_submit_failure:
                                raise ValueError("local update failed")
                            new_state = self.target(args[0], result)
                            assert self.classes[new_state] == "DuelSceneState"
                            self.new_scene_state = dict(self.objects[new_state])
                        elif method == "SetDeckInfo":
                            self.objects[self.styles]["_items"].clear()
                            self.objects[self.styles]["_items"].extend(
                                self.objects[self.target(args[2], result)]["_items"]
                            )
                            self.local_deck = tuple(
                                list(self.objects[self.target(a, result)]["_items"]) for a in args[:2]
                            )
                        else:
                            raise AssertionError("unexpected method " + method)
                    else:
                        raise AssertionError("unexpected op " + kind)
                result.append(self.node(value))
            except TimeoutError:
                raise
            except (ValueError, KeyError, IndexError) as exc:
                return {"ok": False, "error": str(exc), "failed_op": i}
        return {"ok": True, "results": result}
