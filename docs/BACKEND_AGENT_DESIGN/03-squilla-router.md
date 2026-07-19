# 第 3 章：SquillaRouter —— 从请求事实到最终模型

> **本章目标**：把 `apply_squilla_router()` 的完整决策链讲透。重点**不是**宣传"用了哪些 ML 模型"，而是明确：什么情况下不分类、原始分类如何后处理、策略门控如何改变 tier、什么时候真正切换模型、每个结果如何被记录。
>
> 本章把 `apply_squilla_router`（`src/opensquilla/engine/steps/squilla_router.py:1066-1487`，约 420 行）切成 **7 片**完整呈现，再深入 V4 inference、postprocess、RoutingPolicyEngine 的内部细节。

---

## 3.1 路由器到底决定什么

`SquillaRouter` 在一个 turn 的 pre-turn pipeline 中运行（第 2 步 pipeline，详见第 2 章 2.3.1）。它输出的核心**不是**"一个抽象难度分数"，而是一个可以绑定到 provider selector 的**路由决策**：

```text
RoutingDecision
  ├─ tier        c0 / c1 / c2 / c3 / image_model
  ├─ model       该 tier 配置的模型 id
  ├─ confidence  分类置信度
  └─ source      image_route / v4_phase3 / v4_unavailable / router_control_hold / default / ...
```

随后还会派生 thinking/prompt controller 结果，写入 `ctx.metadata`，并由 `PromptAssemblerStage` 把最终 `ctx.model` 应用到本 turn 的 cloned selector（详见 3.9）。

### 3.1.1 必须区分的三个值

| 值 | 含义 | 在哪一层产生 |
|---|---|---|
| **base_tier** | 分类器或显式默认路径**首先提出**的 tier | strategy.classify / default |
| **final_tier** | 后处理、历史、能力、上下文、预算门控**之后**的 tier | RoutingPolicyEngine._finalize |
| **ctx.model** | 在当前 rollout phase 下**实际应用**到 selector 的模型 | apply_squilla_router 末尾 |

> **关键陷阱**：在 observe 阶段，`final_tier` 可以记录为路由结果（如 c2），但文本 turn 的 `ctx.model` 仍保持 baseline model。因此**不能**看到 `routed_tier=c2` 就断言该 turn 实际使用了 c2 模型。必须同时看 `routing_applied` 和 `applied_model`（3.9 节样例）。

---

## 3.2 tier、route class 和 TierConfig

### 3.2.1 canonical tier

源码：`src/opensquilla/router_tiers.py:9-12`

```python
TEXT_TIERS = ("c0", "c1", "c2", "c3")
DEFAULT_TEXT_TIER = "c1"
HIGHEST_TEXT_TIER = "c3"
IMAGE_TIER = "image_model"
```

旧配置中的 `t0`、`t1`、`t2`、`t3` 会被 `normalize_text_tier` 归一化为 `c0` 到 `c3`。策略比较必须使用 canonical ladder 的顺序（`tier_index()`），**而不是**配置文件的字典插入顺序——3.5 节会看到这正是 `apply_squilla_router` 里 `valid_tiers` 排序的原因。

### 3.2.2 route class（模型内部标签）

模型 bundle 内部使用 `R0`、`R1`、`R2`、`R3` 作为分类标签；代码通过 `ROUTE_CLASS_TO_TIER`（`router_tiers.py:21-26`）映射：

```text
R0 <-> c0
R1 <-> c1
R2 <-> c2
R3 <-> c3
```

`route class` 是**模型/推理内部标签**，`tier` 是 OpenSquilla 配置和 provider selector 使用的标识。文档中**不要**把 `R2` 写成一个具体模型名。

### 3.2.3 TierConfig 的真实字段

源码：`src/opensquilla/router_tiers.py:96-129`（`@dataclass(frozen=True)` 在 `:95`）

```text
provider
model
description
thinking_level
supports_image
image_only
```

> **修正旧文档**：模型是否支持 vision、上下文窗口是否足够，后续 capability gate 还会参考 **model catalog** 的确定性事实；它们**不是** `TierConfig` 本身的完整字段。旧文档把 context window 直接归为 `TierConfig` 属性是不准确的。

---

## 3.3 配置和 rollout phase

源码：`src/opensquilla/gateway/config.py:1069-1202`（`class SquillaRouterConfig`）

关键配置：

| 配置 | 默认/语义 |
|---|---|
| `enabled` | 默认启用；关闭时 step 直接返回 |
| `rollout_phase` | `observe` / `prompt_only` / `full` |
| `strategy` | 当前统一归一为 `v4_phase3`；其他旧值会告警并忽略 |
| `default_tier` | 默认 `c1` |
| `tiers` | tier → provider/model/capability 的配置 |
| `confidence_threshold` | confidence gate 的基础阈值，默认 0.5 |
| `routing_timeout_seconds` | 路由 step 的超时，默认 5 秒 |
| `kv_cache_anti_downgrade_enabled` | 默认开启，窗口默认 600 秒 |
| `cross_provider_tiers` | 默认关闭；跨 provider 执行需要额外凭据和连续性检查 |
| `tier_provider_mismatch` | `route` 或 `veto` |
| `budget` | 默认不启用 session spend gate |
| `self_learning` | 反馈/训练/晋升路径，按配置 opt-in |

### 3.3.1 rollout phase 的实际效果

`_apply_controller()` 位于 `engine/steps/squilla_router.py:1023-1052`：

| phase | 文本 model 是否应用 | prompt hint | thinking 控制 |
|---|---|---|---|
| `observe` | 否，记录 would-be route | 不注入 | 只记录 controller metadata |
| `prompt_only` | 是；`routing_applied` 为 true | 注入 P0/P1 hint；P2 仅记录 | 不按 auto-thinking 全量控制 |
| `full` | 是 | 注入 prompt hint | `auto_thinking=true` 时按 controller 记录有效 thinking |

代码中明确 `routing_applied = rollout_phase != "observe"`（`:1373`）。**图片 route 和 router-control hold** 是确定性/操作员控制路径，可能在 observe 阶段也直接应用（3.4、3.5 会看到 `routing_applied=True` 被强制设置）；阅读日志时要结合 `routing_source` 和 `routing_applied`。

---

## 3.4 `apply_squilla_router()` 切片一：配置守卫与短路优先级

从本节开始切 7 片。**第一片：配置守卫 + 短路优先级**。

源码位置：`src/opensquilla/engine/steps/squilla_router.py:1066-1083`

```python
async def apply_squilla_router(ctx: TurnContext) -> TurnContext:
    router_cfg = getattr(ctx.config, "squilla_router", None) if ctx.config else None
    if not router_cfg or not getattr(router_cfg, "enabled", False):
        return ctx

    tiers = getattr(router_cfg, "tiers", {})
    if not tiers:
        return ctx

    semantic_message = getattr(ctx, "semantic_message", None)
    if semantic_message is None:
        semantic_message = getattr(ctx, "raw_message", None)
    if semantic_message is None:
        semantic_message = ctx.message
    if ":subagent:" in ctx.session_key:
        return ctx

    rollout_phase: str = getattr(router_cfg, "rollout_phase", "observe")
```

► **注解（逐行）**
- `:1067` `getattr(ctx.config, "squilla_router", None)`：允许 config 为空或旧配置对象缺少字段。
- `:1068-1069` `enabled=false`：router step 是 no-op，不抛异常，不改变 `ctx.model`。
- `:1071-1073` `tiers` 为空：即使 `enabled=true`，也没有合法模型候选，直接 no-op。
- `:1075-1079` `semantic_message` 优先取 property；如果某个测试对象没有 property，就回退 `raw_message`，再回退 `message`。
- `:1080-1081` `:subagent:` 是**硬短路**。子 Agent 使用父级传入的运行模型/上下文，**不应该**重新根据子任务文本路由。
- `:1083` `rollout_phase` 此时只保存，**不立即决定**是否应用；图片和 hold 路径可能绕过 observe。

这段代码的隐含优先级可以写成：

```text
配置不存在/关闭
  > tiers 不存在
    > subagent
      > image gate（3.5）
        > empty text（3.5 末尾）
          > hold（3.6）
            > ML classify（3.7）
```

> **设计动机**：优先级**不是**装饰。把 empty-text guard 放到 image gate 前，会导致**空 caption 图片**被错误地当成"不需要路由"（3.5 的案例 B 会验证这点）。

---

## 3.5 `apply_squilla_router()` 切片二：图片路径为什么跳过 ML

源码位置：`src/opensquilla/engine/steps/squilla_router.py:1085-1158`

```python
    # Image-aware routing: skip ML and pick directly from supports_image tiers
    # for current uploads. Historical images require the upstream semantic
    # follow-up gate; recent-image/sticky metadata alone is observability and
    # replay context, not enough to force vision.
    #
    # This runs BEFORE the empty-text guard below: the image route is
    # deterministic and never consumes the message text, so an image turn with
    # an empty/whitespace caption must still be routed to a vision tier rather
    # than falling through the empty-text early return.
    current_turn_has_image = _attachments_include_image(ctx.attachments)
    history_gate_needs_image = (
        ctx.metadata.get("router_vision_followup_needs_image") is True
    )
    # Computed once and reused below by both the bypass and the policy
    # engine's capability gate (which must not recompute the signal). On the
    # classify path this is always False today — the bypass routes or raises
    # for every image turn — which is exactly the gate's no-op default.
    turn_needs_image = current_turn_has_image or history_gate_needs_image
    if turn_needs_image:
        image_tiers = {k: v for k, v in tiers.items() if v.get("supports_image", False)}
        if not image_tiers:
            log.warning(
                "squilla_router.no_image_tier",
                note="image detected but no supports_image tier",
            )
            raise RuntimeError(
                "No image-capable SquillaRouter tier is configured for this image request. "
                "Configure squilla_router.tiers.image_model with supports_image=true."
            )
        tier_name = next(iter(image_tiers))
        decision = RoutingDecision(
            tier=tier_name,
            model=image_tiers[tier_name].get("model", ctx.model),
            confidence=1.0,
            source="image_route",
        )
        # Vision turns are not just a text-tier routing decision: they require a
        # model that can consume image blocks. Apply this route even during
        # observe rollout so multimodal requests do not remain on a text tier.
        routing_applied = True
        ctx.metadata["baseline_model"] = ctx.model
        if routing_applied:
            ctx.model = decision.model
        ctx.metadata["routed_tier"] = decision.tier
        ctx.metadata["routed_model"] = decision.model
        ctx.metadata["routing_applied"] = routing_applied
        ctx.metadata["rollout_phase"] = rollout_phase
        ctx.metadata["applied_model"] = ctx.model
        ctx.metadata["routing_confidence"] = decision.confidence
        ctx.metadata["routing_source"] = decision.source
        image_route_reason = "current_turn" if current_turn_has_image else "gate_history"
        ctx.metadata["image_route_reason"] = image_route_reason
        history_turns = 1
        if image_route_reason == "gate_history":
            history_turns = max(
                1,
                int(getattr(router_cfg, "vision_history_lookback_turns", 8) or 1),
            )
        ctx.metadata["route_max_history_turns"] = history_turns
        ctx.metadata.update(_compute_savings(decision.model, tiers))
        # Record the image tier's provider (and assess cross-provider/mismatch)
        # like the hold and classify paths — without this, a vision tier that
        # declares provider=X never executes the cross-provider switch and no
        # mismatch telemetry is emitted.
        _flag_tier_provider_mismatch(ctx, tiers, decision.tier, routing_applied=True)
        _record_thinking_metadata(ctx, router_cfg, image_tiers[tier_name])
        stage_router_decision(ctx, decision=decision)
        log.debug("squilla_router.image_routed", tier=decision.tier, model=decision.model)
        return ctx

    # Empty-text guard for the ML text classifier: only reached for non-image
    # turns (the vision bypass above already handled empty-caption images).
    if not semantic_message.strip():
        return ctx
```

► **注解（逐行）**
1. `:1094` `current_turn_has_image` 只检测**当前附件**（`_attachments_include_image` 在 `:1055-1063`，检查 type/mime/media_type/mime_type 是否为 `image/*`）。
2. `:1095-1097` `history_gate_needs_image` 是上游 vision follow-up gate 的结论（`apply_vision_followup_gate`，定义在 `engine/steps/vision_followup_gate.py:287-307`），**不是**"历史里有图片"本身。注释 `:1085-1088` 明确：recent-image/sticky metadata 只是 observability，不足以强制 vision。
3. `:1102` `turn_needs_image` 只计算**一次**，后面 policy capability gate 复用同一个事实，避免上游和下游判断不一致。
4. `:1104` `image_tiers` 只依据 `supports_image=true` 筛选，**不看**文本 classifier。
5. `:1105-1113` 无 image tier 时抛 `RuntimeError`，**而不是**悄悄把图片交给文本模型。注意它还先 `log.warning`——让 pipeline 的 fail-open 能捕获并记录。
6. `:1114` `next(iter(image_tiers))`：当前实现选**第一个**符合条件的 image tier。如果配置了多个 `supports_image` tier，配置顺序可能影响选择。
7. `:1118` `confidence=1.0`：这是**确定性** capability route，**不是** ML 置信度。
8. `:1124` `routing_applied = True`：**被强制设为 true**，即使 `rollout_phase=observe`；视觉请求不能因为 observe 留在文本模型上（注释 `:1121-1123`）。
9. `:1125` `baseline_model` 记录原本的 model，方便解释发生了什么切换。
10. `:1135-1136` `image_route_reason` 区分"本 turn 有图"还是"历史 gate 要求图"。
11. `:1144` `_compute_savings` 计算从 baseline 切到 decision.model 的成本节省 metadata。
12. `:1149` `_flag_tier_provider_mismatch`：vision tier 也走 provider mismatch 检查（3.10）。
13. `:1151` `stage_router_decision`：写入统一 decision record（定义在 `engine/steps/router_decision_record.py:190-239`）。
14. `:1153` `return ctx`：**立即返回**，不进入后面的文本分类流程。
15. `:1157-1158` empty-text guard：**只在非图片 turn** 才检查（注释 `:1155-1156`）。

> **关键结论**：`next(iter(image_tiers))` 选第一个 image tier，**不是**在所有 vision tier 之间执行 ML 竞争。如果需要成本/能力排序，应在这里显式增加 deterministic ordering，不能误以为已经存在最优视觉路由。

### 3.5.1 真实数据样例：图片路由 metadata

```json
// 输入：message=" ", attachments=[{"mime": "image/png"}], rollout_phase="observe"
// 输出 ctx.metadata：
{
  "baseline_model": "gpt-4o-mini",
  "routed_tier": "image_model",
  "routed_model": "gpt-4o",
  "routing_applied": true,           // ← observe 也强制 true
  "applied_model": "gpt-4o",
  "routing_source": "image_route",
  "routing_confidence": 1.0,         // ← 确定性路由，非 ML
  "image_route_reason": "current_turn",
  "route_max_history_turns": 1
}
```

---

## 3.6 `apply_squilla_router()` 切片三：valid_tiers 规范化 + hold 确定性覆盖

源码位置：`src/opensquilla/engine/steps/squilla_router.py:1160-1211`

```python
    # Order valid_tiers by the canonical c0<c1<c2<c3 ladder rather than TOML
    # insertion order — downstream policy stages rank tiers by position in this
    # list, so trusting declaration order inverted upgrades/holds for configs
    # that list tiers out of order. Unknown/custom tier names (tier_index == -1)
    # sort after the canonical ones, preserving their relative order (stable).
    valid_tiers = [name for name, tier in tiers.items() if not tier.get("image_only", False)]
    valid_tiers = sorted(
        valid_tiers,
        key=lambda name: (0, tier_index(name)) if tier_index(name) >= 0 else (1, 0),
    )
    if not valid_tiers:
        return ctx

    hold_store = ctx.metadata.get("router_control_hold_store")
    if isinstance(hold_store, RouterControlHoldStore):
        hold = hold_store.get_valid(ctx.session_key, decrement=True)
        if hold is not None and hold.tier in tiers and hold.tier in valid_tiers:
            decision = RoutingDecision(
                tier=hold.tier,
                model=hold.model,
                confidence=1.0,
                source="router_control_hold",
            )
            ctx.metadata["baseline_model"] = ctx.model
            ctx.model = decision.model
            ctx.metadata["routed_tier"] = decision.tier
            ctx.metadata["routed_model"] = decision.model
            ctx.metadata["routing_applied"] = True
            ctx.metadata["applied_model"] = ctx.model
            ctx.metadata["routing_confidence"] = decision.confidence
            ctx.metadata["routing_source"] = decision.source
            ctx.metadata["router_fallback_chain"] = _router_text_fallback_chain(
                decision.tier,
                tiers,
            )
            ctx.metadata["router_control_hold_applied"] = True
            ctx.metadata["router_control_action"] = "set_hold"
            ctx.metadata["router_control_target_tier"] = hold.tier
            ctx.metadata["router_control_target_model"] = hold.model
            ctx.metadata["router_control_target_provider"] = hold.provider
            ctx.metadata["router_control_evidence"] = hold.evidence
            ctx.metadata.update(_compute_savings(decision.model, tiers))
            _flag_tier_provider_mismatch(ctx, tiers, decision.tier, routing_applied=True)
            _record_thinking_metadata(ctx, router_cfg, tiers[decision.tier])
            stage_router_decision(ctx, decision=decision)
            log.debug(
                "squilla_router.router_control_hold_applied",
                tier=decision.tier,
                model=decision.model,
                session=ctx.session_key,
            )
            return ctx
```

► **注解（valid_tiers）**
- `:1165` `image_only` tier 从文本分类候选中移除。
- `:1166-1169` `sorted` **不信任** TOML/dict 声明顺序。`c0/c1/c2/c3` 通过 `tier_index` 排成 canonical ladder；未知自定义 tier 的 `tier_index=-1`，被放到 canonical tier 后面，同时保持稳定顺序。
- `:1170-1171` `valid_tiers` 为空时无法文本路由，直接返回原 ctx。

> **设计动机**：为什么配置顺序不能作为 tier 等级？

```text
错误配置顺序：c3, c0, c2, c1
如果直接按 list index 比较：
  c0 可能被当成最高/最低的错误位置
  策略升级、anti-downgrade、budget cap 全部可能反转
```

任何新增 policy 都必须使用 `tier_index` 或 policy 的 canonical ordering，**不能**直接用 `tiers.keys()` 的位置。

► **注解（hold）**
- `:1175` `get_valid(..., decrement=True)`：hold 在本次有效消费时**递减**；**不是**读取但无限期保持。
- `:1176` hold 必须同时存在于 `tiers` 和 `valid_tiers`，**不能** hold 到 image_only 或不存在的 tier。
- hold **不调用** V4、**不经过** ML、**不经过** complaint/anti-downgrade 的分类阶段。
- `:1190` `source="router_control_hold"`：让日志明确知道这是**操作员控制**，不是模型分类。
- `:1191-1194` fallback chain 仍然被生成，便于后续 provider 层做允许的低 tier 回退。
- `:1195-1200` 写入完整的 router-control 证据元数据（action/target_tier/target_model/target_provider/evidence）。

> **关键结论**：如果调试时看到 `confidence=1.0`，**不能**直接推断"ML 非常确定"；还要查看 `routing_source` 是 `image_route` 还是 `router_control_hold`。

---

## 3.7 `apply_squilla_router()` 切片四：历史加载与分类

源码位置：`src/opensquilla/engine/steps/squilla_router.py:1213-1303`

```python
    strategy = _get_strategy(router_cfg)
    strategy_name = _strategy_name(router_cfg)
    defer_history = bool(ctx.metadata.get(_DEFER_ROUTING_HISTORY_KEY))

    # History-aware routers load accumulated routing history for this session.
    routing_history = None
    if _is_history_strategy(strategy_name):
        stored_history = _history_store.get(ctx.session_key)
        routing_history = [dict(entry) for entry in stored_history or []] or None
        if not routing_history:
            persisted = ctx.metadata.get("routing_history")
            if persisted:
                now = time.monotonic()
                routing_history = [
                    {**dict(entry), "_ts": now} if "_ts" not in entry else dict(entry)
                    for entry in persisted
                    if isinstance(entry, dict)
                ]
                if not defer_history:
                    _history_store.set(ctx.session_key, routing_history)
                    log.debug(
                        "squilla_router.history_cold_start",
                        session=ctx.session_key,
                        restored=len(routing_history),
                    )
        if routing_history:
            cutoff = time.monotonic() - _ROUTING_HISTORY_WINDOW
            routing_history = [e for e in routing_history if e.get("_ts", 0) > cutoff]
            routing_history = routing_history[-_MAX_ROUTING_HISTORY:]
            if not defer_history:
                _history_store.set(ctx.session_key, routing_history)
        log.debug(
            "squilla_router.history_loaded",
            session=ctx.session_key,
            history_len=len(routing_history) if routing_history else 0,
        )

    # --- Classification ---
    thinking_mode: str | None = None
    prompt_policy: str | None = None
    extra: dict | None = None
    probs: list[float] | None = None

    classify_context = _classify_context_kwargs(
        strategy,
        {
            "prev_assistant_text": ctx.metadata.get("router_prev_assistant_text"),
            "prev_assistant_usage": ctx.metadata.get("router_prev_assistant_usage"),
            "history_user_texts": ctx.metadata.get("router_history_user_texts"),
            "flags_text_override": ctx.metadata.get("router_flags_text_override"),
            # Non-image attachments only: image turns were routed to a vision
            # tier before classification. Signature-filtered like the other
            # context keys, so strategies that don't declare it never see it.
            "attachment_count": len(ctx.attachments or []),
        },
    )
    tier_name, confidence, source, extra = await strategy.classify(
        semantic_message,
        valid_tiers,
        routing_history=routing_history,
        **classify_context,
    )
    tier_name = normalize_text_tier(tier_name) or tier_name
    if extra:
        ctx.metadata["routing_extra"] = extra
        thinking_mode = extra.get("thinking_mode")
        prompt_policy = extra.get("prompt_policy")
        # Move the (large) self-learning feature vectors out of routing_extra so
        # they never reach decision logs or accumulated routing history.
        train_features = extra.pop("_train_features", None)
        if train_features is not None:
            ctx.metadata["routing_train_features"] = train_features
            ctx.metadata["routing_train_turn_index"] = len(routing_history or [])

    if tier_name is None or tier_name not in tiers:
        default = normalize_text_tier(getattr(router_cfg, "default_tier", DEFAULT_TEXT_TIER))
        if default is None:
            default = DEFAULT_TEXT_TIER
        tier_name = default if default in tiers else next(iter(tiers), None)
        if tier_name is None:
            return ctx
        confidence = 0.0
        source = "default"
        probs = synthetic_one_hot(tier_name)

    decision = RoutingDecision(
        tier=tier_name,
        model=tiers[tier_name].get("model", ctx.model),
        confidence=confidence,
        source=source,
    )

    ctx.metadata["baseline_model"] = ctx.model
```

► **注解（逐段）**
1. `:1213` `_get_strategy`（`:437-449`）返回缓存的策略对象；bundle 加载**不在每个 turn 重做**。
2. `:1214` `strategy_name` 当前会归一为 `v4_phase3`；历史策略因此能加载 routing history。
3. `:1219-1248` 历史加载：进程内 `_history_store` 优先；缺失时从 `ctx.metadata` 的 persisted `routing_history` cold start（`:1222-1237`）。历史按时间窗口（`_ROUTING_HISTORY_WINDOW`）和最大条数（`_MAX_ROUTING_HISTORY`）裁剪，避免无限增长。
4. `:1256-1268` `_classify_context_kwargs` 会根据 `strategy.classify` 的签名**过滤**参数。策略不声明的上下文不会被强行传入。
5. `:1266` `attachment_count` 只给非图片分类路径；图片已经在前面短路。
6. `:1269-1274` `classify` 返回四元组：`tier`、`confidence`、`source`、`extra`。`extra` 可能包含 probabilities、flags、thinking_mode、prompt_policy、features 和 trail。
7. `:1275` `normalize_text_tier(tier_name)` 把 `t0..t3` 归一为 `c0..c3`。
8. `:1282-1285` `pop("_train_features")` 是一个 **token/日志保护边界**：训练特征可能很大，供离线训练使用，**不应**进入普通 `routing_extra`（`routing_extra` 会进 decision log 和 routing history）。
9. `:1287-1296` 分类器返回未知 tier 的 default 路径：`default_tier` 也无效时尝试第一个配置 tier；连 tiers 都没有时返回原 ctx。`confidence=0.0` 明确标记这是 fallback，**不是**模型自信。`synthetic_one_hot` 只为后续 controller 提供安全的概率形状。
10. `:1305` `baseline_model` 记录，供后续解释切换。

---

## 3.8 `apply_squilla_router()` 切片五：controller 补全与 policy run

源码位置：`src/opensquilla/engine/steps/squilla_router.py:1307-1371`

```python
    # --- Controller: derive thinking_mode / prompt_policy if v4 returned no head decisions ---
    if thinking_mode is None and probs is not None:
        try:
            flags = extra.get("flags") if extra else None
            thinking_mode = derive_thinking_mode(probs, flags)
            prompt_policy = derive_prompt_policy(probs, flags)
            thinking_mode, prompt_policy = normalize_decisions(thinking_mode, prompt_policy)
            if decision.source in {"v4_unavailable", "default"} and prompt_policy == "P0":
                prompt_policy = "P1"
        except Exception:
            log.warning("squilla_router.controller_error", exc_info=True)
            thinking_mode = None
            prompt_policy = None

    # --- Apply decisions: post-classifier policy stages -----------------------
    # The policy engine consumes plain data only; context gathering stays here.
    if _is_history_strategy(strategy_name):
        routing_extra = ctx.metadata.setdefault("routing_extra", extra or {})
    else:
        routing_extra = ctx.metadata.get("routing_extra")
    material_estimated_tokens = _material_estimated_tokens(ctx, semantic_message)
    budget_input = _budget_gate_input_for_turn(
        ctx,
        router_cfg,
        routed_model=decision.model,
        material_tokens=material_estimated_tokens,
    )
    if budget_input is not None and budget_input.spend_usd is None:
        # Active gate but the accumulated spend (or a required forward price)
        # could not be determined: suspend rather than act on missing data.
        log.debug(
            "router_budget.suspended",
            session=ctx.session_key,
            limit_usd=budget_input.limit_usd,
            spend_source=budget_input.spend_source,
        )
    policy_result = _POLICY_ENGINE.run(
        PolicyInputs(
            decision=decision,
            message=semantic_message,
            router_cfg=router_cfg,
            tiers=tiers,
            valid_tiers=valid_tiers,
            routing_history=routing_history,
            extra=routing_extra if isinstance(routing_extra, dict) else None,
            thinking_mode=thinking_mode,
            prompt_policy=prompt_policy,
            history_strategy=_is_history_strategy(strategy_name),
            material_estimated_tokens=material_estimated_tokens,
            context_window_tokens=_context_window_tokens(ctx, router_cfg),
            turn_has_image=turn_needs_image,
            tier_capabilities=_tier_capability_facts(
                tiers,
                valid_tiers,
                str(getattr(getattr(ctx.config, "llm", None), "provider", "") or ""),
            ),
            calibration=_calibration_for_turn(router_cfg),
            budget=budget_input,
        )
    )
    decision = policy_result.decision
    thinking_mode = policy_result.thinking_mode
    prompt_policy = policy_result.prompt_policy
    ctx.metadata.update(policy_result.metadata_updates)
    _log_budget_outcome(ctx)
```

► **注解**
- `:1308-1319` controller 补全：当 V4 没返回 head decisions（heuristic/default 路径），用 `derive_thinking_mode`（`controller.py:76`）和 `derive_prompt_policy`（`controller.py:100`）从 probs 派生。`normalize_decisions`（`:120`）禁止 T2/T3 + P0 这种矛盾组合。`v4_unavailable`/`default` 路径强制 P0→P1（避免 fallback 还压 prompt）。
- `:1327` `material_estimated_tokens`：当前 turn 内容/附件的 material token 估算。
- `:1334-1342` budget 挂起：active gate 但 spend 未知时，**挂起不动作**，而不是凭缺失数据改路由。
- `:1343-1366` `PolicyInputs` 同时携带一整套运行时事实（见下表）。
- `:1367-1371` policy 后的 decision/thinking_mode/prompt_policy 才是最终值；`metadata_updates` 由调用方统一 `update`。

`PolicyInputs` 的字段来源：

| 输入 | 来源 | 用途 |
|---|---|---|
| `decision` | strategy.classify | 当前候选 tier/model/confidence |
| `routing_history` | history store/metadata | complaint、anti-downgrade、previous final tier |
| `material_estimated_tokens` | 当前 turn 内容/附件 | large context floor、capability |
| `context_window_tokens` | config/catalog | 计算上下文下限 |
| `turn_has_image` | 前置视觉判断（`:1102`） | capability gate |
| `tier_capabilities` | model catalog | vision/context 硬事实 |
| `calibration` | calibration service | confidence gate 调整 |
| `budget` | spend/pricing | warn 或 cap |

> **关键结论**：这解释了为什么"ML 返回 c1"**不是**最终结果——policy 输入还包含一整套运行时事实。

---

## 3.9 `apply_squilla_router()` 切片六：最终 model 和 metadata 写入

源码位置：`src/opensquilla/engine/steps/squilla_router.py:1373-1413`（含后续 provider state continuity）

```python
    routing_applied = rollout_phase != "observe"
    decision, thinking_mode, prompt_policy = _apply_provider_mismatch_veto(
        ctx,
        router_cfg,
        tiers,
        valid_tiers,
        decision,
        thinking_mode,
        prompt_policy,
        routing_applied=routing_applied,
    )
    if routing_applied:
        ctx.model = decision.model
    ctx.metadata["routed_tier"] = decision.tier
    ctx.metadata["routed_model"] = decision.model
    ctx.metadata["routing_applied"] = routing_applied
    ctx.metadata["rollout_phase"] = rollout_phase
    ctx.metadata["applied_model"] = ctx.model
    ctx.metadata["routing_confidence"] = decision.confidence
    ctx.metadata["routing_source"] = decision.source
    ctx.metadata["router_fallback_chain"] = _router_text_fallback_chain(
        decision.tier,
        tiers,
    )
    ctx.metadata.update(_compute_savings(decision.model, tiers))
    _flag_tier_provider_mismatch(ctx, tiers, decision.tier, routing_applied=routing_applied)

    context_states = ctx.metadata.get("session_context_states") or ctx.metadata.get(
        "active_context_states"
    )
    if isinstance(context_states, list):
        tier_cfg = tiers[decision.tier]
        candidate_provider = str(
            tier_cfg.get("provider") or getattr(router_cfg, "tier_profile", "") or ""
        )
        ctx.metadata["provider_state_continuity"] = provider_state_continuity_diagnostic(
            context_states=context_states,
            candidate_provider=candidate_provider,
            candidate_model=decision.model,
            now_ms=int(time.time() * 1000),
        ).as_metadata()
```

► **注解（逐行）**
- `:1373` `routing_applied = rollout_phase != "observe"`：**只有**这条路径的 routing_applied 受 rollout 控制（图片/hold 路径前面已强制 True）。
- `:1374-1383` `_apply_provider_mismatch_veto`（`:958-1022`）可以在 policy 后**再次改变** decision（见 3.10）。
- `:1384-1385` observe 下**不写** `ctx.model`，因此 `applied_model` 仍是 baseline。
- `:1386` `routed_tier` = policy + veto 后的最终 tier。
- `:1387` `routed_model` = 希望路由到的模型。
- `:1389` `rollout_phase` 写入，方便日志解释 applied 为何是 false。
- `:1390` `applied_model` = 实际写入 ctx.model 的模型（observe 时 ≠ routed_model）。
- `:1393-1396` fallback chain 依据**最终** `decision.tier` 生成，而非原始 tier。
- `:1400-1413` provider state continuity：跨 provider 切换时的连续性诊断。

> **建议**：所有路由日志至少打印 `source / base_tier / final_tier / routed_model / applied_model / routing_applied / confidence / routing_trail`。否则只打印 tier 会无法判断它是 ML 结果、policy 强制结果还是 observe 结果。

### 3.9.1 真实数据样例：observe vs full 的 metadata 差异

```json
// 场景：classifier 选 c2，rollout_phase="observe"
{
  "routed_tier": "c2",
  "routed_model": "claude-3-5-sonnet",
  "routing_applied": false,          // ← observe 不应用
  "applied_model": "gpt-4o-mini",    // ← 仍是 baseline！
  "rollout_phase": "observe"
}

// 同一分类结果，rollout_phase="full"
{
  "routed_tier": "c2",
  "routed_model": "claude-3-5-sonnet",
  "routing_applied": true,           // ← full 应用
  "applied_model": "claude-3-5-sonnet",  // ← 切换了
  "rollout_phase": "full"
}
```

---

## 3.10 `apply_squilla_router()` 切片七：provider mismatch 与 fallback chain

### 3.10.1 三种 provider mismatch 行为

路由 tier 可以声明 provider，但"tier 写了 provider"**不等于**当前 turn 已经使用该 provider。

| 配置 | 行为 |
|---|---|
| `cross_provider_tiers=false`（默认） | **route-and-flag**：记录 mismatch，仍可能用 active provider 的 credentials 执行该 model id。运行日志必须显式暴露 mismatch。 |
| `cross_provider_tiers=true` | 执行跨 provider tier 需要：路由确实应用 + 能从 profile/env 解析凭据 + provider-bound state continuity 允许切换 + cloned selector 使用完整目标 ProviderConfig。连续性诊断要求丢弃不可移植 state 时，切换被阻止。 |
| `tier_provider_mismatch="veto"` | mismatch 后寻找最接近、实际运行在 active provider credentials 上的 tier；找不到时尝试 default tier；仍无法 rebind 则回到 route-and-flag。veto 会记录 routing trail，**不静默**改写。 |

`_apply_provider_mismatch_veto`（`:958-1022`）和 `_flag_tier_provider_mismatch`（image/hold/classify 三条路径都调）共同实现这三种行为。

### 3.10.2 fallback chain 向哪里回退

`_router_text_fallback_chain()` 位于 `engine/steps/squilla_router.py:83`。它从选定 tier 的**下方**构造链，并按"接近选定 tier 到最便宜 tier"的顺序加入：

```text
selected = c3
fallback chain = [c2, c1, c0]
```

> **关键结论**：这是一条**向更便宜 tier 的回退链**，**不是**"provider 失败后不断升级到更强模型"。它会随 `routing_applied=true` 写入 metadata，由 selector/provider 侧在允许的路径中使用。跨 provider 执行时这条同 provider fallback chain 可能被跳过（它属于正在离开的 provider）。

---

## 3.11 策略运行时与降级链

### 3.11.1 策略缓存

`_get_strategy()` 位于 `engine/steps/squilla_router.py:437-449`。缓存 key 包含 strategy、bundle、aux head、runtime requirement、confidence 和 capture flags。active bundle 变化或配置 key 变化时会清理旧策略/历史；self-learning promotion/rollback 可以调用 `invalidate_strategy_cache()` 让下一次 turn 重新加载。

### 3.11.2 实际的加载顺序

```text
learned active bundle（如果存在）
    -> shipped/base V4 Phase 3 bundle
        -> HeuristicRouterStrategy
            -> _UnavailableV4Strategy（default-only）
```

> **设计动机（解释）**：这里有一个重要细节——learned bundle 加载失败时，**先回退到 shipped baseline**；**不会**直接跳到 heuristic。只有 baseline 也不可用，才进入 heuristic；heuristic 创建失败，最后才是 default-only。

路由是**优化能力而非 Agent 执行前提**。即使 V4 bundle、ONNX runtime 或 LightGBM 依赖不可用，最终也应保留默认 tier/模型路径，不让路由运行时故障阻断 turn。`require_router_runtime` 影响运行时故障的暴露方式，但**不改变**"执行链需要可用默认路径"的原则（第 1 章 1.11 不变量 3）。

---

## 3.12 V4 推理：特征、头和融合的真实关系

V4 入口：`src/opensquilla/squilla_router/v4_phase3.py:62`（`class V4Phase3Strategy`）
核心推理类：`models/v4.2_phase3_inference/runtime_src/src/router/inference/core.py:14`

```text
InferenceRequest
  -> build_feature_bundle
       v3/传统特征 + BGE 特征（如果 bundle 配置启用）
  -> run_heads
       p_main_lgbm
       p_mlp_calibrated
       p_aux_lgbm（可选）
  -> fuse_probabilities(p_main_lgbm, p_mlp_calibrated, alpha)
  -> apply_postprocess(fused_probs, aux_probs, request, config)
  -> FinalDecision
```

### 3.12.1 `InferenceCore.predict` 完整代码

源码：`core.py:63-87`

```python
    def predict(self, request: InferenceRequest) -> InferenceResult:
        bundle = self._build_features(request)
        outputs = self._run_heads(bundle)
        fused = self._fuse(outputs)
        decision = self._postprocess(fused, outputs.p_aux_lgbm, request)
        intermediates = {
            "bge_channels_used": bundle.bge_channels_used,
            "asst_signal_present": bundle.asst_signal_present,
        }
        # Self-learning capture (opt-in): surface the feature vectors the model
        # actually consumed so an offline trainer reuses them verbatim (no
        # re-extraction, no train/serve skew). Gated so default runs pay nothing.
        if self.config.get("emit_train_features"):
            intermediates["features_390"] = bundle.features_390
            if self.config.get("emit_raw_bge"):
                intermediates["raw_bge_1536"] = bundle.raw_bge_1536
        return InferenceResult(
            decision=decision,
            probabilities={
                route_class: float(fused[idx])
                for idx, route_class in enumerate(ROUTE_CLASSES)
            },
            aux_decision_probs=self._aux_probs_dict(outputs.p_aux_lgbm),
            intermediates=intermediates,
        )
```

► **注解**
1. `_build_features`：把 request 转成模型实际消费的 feature bundle。
2. `_run_heads`：同时得到主 LGBM、MLP calibrated 和可选 aux 输出。
3. `_fuse`：**只融合主 LGBM 与 MLP calibrated**。
4. `_postprocess`：把 fused 概率和 aux 行为概率一起交给后处理。
5. `intermediates`：保存诊断/训练所需的中间数据。
6. `emit_train_features` 默认关闭；开启后才暴露 `features_390`。
7. `emit_raw_bge` 更重，只有二级开关开启才放 `raw_bge_1536`。
8. `probabilities` 是 **fused** 概率，**不是** aux 概率。
9. `aux_decision_probs` 单独保存 initial/maintain/upgrade/downgrade。
10. `decision` 是后处理后的最终模型内 route decision。

### 3.12.2 aux 头不是第三个同权重分类器

源码的 `_fuse()` 只把 `p_main_lgbm` 和 `p_mlp_calibrated` 传给 `fuse_probabilities()`。`p_aux_lgbm` 单独传入 postprocess，用于可选的 aux downgrade 规则，预测的是 **initial/maintain/upgrade/downgrade 行为信号**。

因此下面这种说法**不准确**：

```text
LGBM main + LGBM aux + MLP 三头等权融合成四类概率
```

准确说法是：

```text
主分类 LGBM + MLP calibrated 概率 -> 主 fused_probs
aux LGBM 行为信号 -> postprocess 中可选 downgrade
```

BGE 是否存在、是否使用哪些 channel 由 bundle/runtime 配置和 feature bundle 决定，**不能**仅凭"BGE 1536 维"断言每次部署都完整执行相同特征路径。

---

## 3.13 postprocess：从原始概率到 route class

实现：`src/opensquilla/squilla_router/models/v4.2_phase3_inference/runtime_src/src/router/inference/postprocess.py:157-213`（**完整贴出**）

```python
def apply_postprocess(
    fused_probs: np.ndarray,
    aux_probs: Mapping[str, float] | None,
    request: InferenceRequest,
    config: dict,
) -> FinalDecision:
    fused_probs = np.asarray(fused_probs, dtype=np.float64)
    if fused_probs.shape != (4,):
        raise ValueError("postprocess expects a 4-class probability vector")

    idx = int(np.argmax(fused_probs))
    route = ROUTE_CLASSES[idx]
    sorted_p = np.sort(fused_probs)[::-1]
    margin = float(sorted_p[0] - sorted_p[1])
    difficulty = float(np.dot(fused_probs, np.arange(4, dtype=np.float64)))

    pre_upgrade_route = route
    route = _apply_margin_upgrade(route, margin, config)
    margin_upgraded = route != pre_upgrade_route
    if margin_upgraded:
        aux_downgrade_applied = False
    else:
        route, aux_downgrade_applied = _apply_aux_downgrade(route, aux_probs, config)
    route = _apply_r1_rescue(route, fused_probs, config)
    route = _apply_under_routing_safety(route, fused_probs, config)

    flags_text = (
        request.flags_text_override
        if request.flags_text_override is not None
        else request.current_user_text
    )
    context = _context_from_request(request)
    flags = compute_flags(flags_text, config, context=context)
    route = _apply_flag_overrides(route, flags, config)
    route = _apply_context_rules(route, context, config)
    route, sticky_applied = _apply_optional_sticky_tier(
        route, fused_probs, request, config
    )

    thinking_mode = _derive_thinking_mode(route, margin, flags, config)
    prompt_policy = _derive_prompt_policy(difficulty, margin, flags, config)
    if route == "R0" and _is_trivial_ack(flags_text):
        thinking_mode = "T0"
        prompt_policy = "P0"
    _, selected_model = _select_model(route, config)

    return FinalDecision(
        route_class=route,
        margin=margin,
        difficulty_score=difficulty,
        flags=asdict(flags),
        thinking_mode=thinking_mode,
        prompt_policy=prompt_policy,
        selected_model=selected_model,
        aux_downgrade_applied=aux_downgrade_applied,
        sticky_applied=sticky_applied,
    )
```

### 3.13.1 postprocess 的实际顺序

```text
fused_probs = [p_R0, p_R1, p_R2, p_R3]
    1. argmax -> 初始 route                  (:167-168)
    2. margin upgrade                        (:173-175)  margin = top1 - top2
    3. 若没有 margin upgrade，按配置应用 aux downgrade  (:176-179)
    4. R1 rescue                             (:180)
    5. under-routing safety（重类概率质量过高时至少 R2）  (:181)
    6. flag overrides                        (:190)
    7. context rules（如深度会话最低 R1）        (:191)
    8. optional sticky tier                  (:192-194)
    9. 根据最终 route 推导 thinking_mode / prompt_policy  (:196-197)
```

► **注解（逐分支）**
- `:164-165` `shape != (4,)`：模型输出维度错误直接失败；**不能**用错误维度继续选 tier。
- `:167` argmax：得到 base route。
- `:170` `margin = sorted_p[0] - sorted_p[1]`：margin 只衡量 top1/top2 分离，**不是**概率本身。margin 小表示分类边界不确定，`_apply_margin_upgrade()` 可以按配置将 tier **向上**提升。
- `:171` `difficulty`：按 R0/R1/R2/R3 的索引加权求期望难度。
- `:176-177` 如果 margin upgrade 已经升级，则**禁止**随后 aux downgrade 抵消这次安全升级。
- `:178-179` 只有**没有** margin upgrade 时，aux downgrade 才有机会**向下**调整。它默认由 `v4.aux_downgrade.enabled` 控制，R0 不再向下。
- `:180` `_apply_r1_rescue()`：处理低端边界。
- `:181` `_apply_under_routing_safety()`：检查当前低于 R2 时的 `p_R2 + p_R3`。默认安全阈值 0.45（实际阈值来自 bundle config）：超过阈值会强制到 R2。安全网只解决"模型概率显示高风险但 argmax 偏低"的一类问题，**不能**证明分类结果正确。
- `:183-187` `flags_text` 可以使用 `flags_text_override`，而**不是**始终使用当前 user text。
- `:191` context rules 在 sticky **之前**执行。当前可以按 `turn_index` 设置深度对话最低类别，默认深度阈值 4、最低 R1。
- `:192-194` `_apply_optional_sticky_tier()` 只有 `v4.sticky_tier.enabled=true` 时才启用。启用后，当前预测低于最近 route decision 时可以保持较高 tier，以减少 provider prefix/KV cache 因模型降级而失效。它**不是**无条件的全局"永不降级"规则；引擎层还有独立的 anti-downgrade window（3.14）。
- `:196-197` `thinking_mode` 根据最终 route，但 `prompt_policy` 使用 difficulty/margin/flags，**不只是** tier 映射。
- `:198-200` R0 trivial acknowledgement 还有特殊的 T0/P0 快路径。

### 3.13.2 controller 的 normalize

`controller.py:76` 的 `derive_thinking_mode()` 和 `:100` 的 `derive_prompt_policy()` 根据最终 route、difficulty/margin 和 flags 派生：

```text
thinking_mode: T0 / T1 / T2 / T3
prompt_policy: P0 / P1 / P2
```

`normalize_decisions()`（`:120-124`）**禁止** T2/T3 + P0 这种"深度思考但压缩提示"的矛盾组合，必要时把 prompt policy 提升到 P1。

---

## 3.14 RoutingPolicyEngine：模型之后还有一层硬策略

实现：`src/opensquilla/engine/routing/policy.py:932-1073`

ML/postprocess 输出**只是 policy 的输入**，**不是**最终决策。

### 3.14.1 `run()` 完整代码

源码：`policy.py:941-994`

```python
    def run(self, inputs: PolicyInputs) -> PolicyResult:
        decision = inputs.decision
        thinking_mode = inputs.thinking_mode
        prompt_policy = inputs.prompt_policy
        metadata_updates: dict = {}
        extra = inputs.extra if isinstance(inputs.extra, dict) else None

        if inputs.history_strategy and extra is not None:
            decision = self._finalize(decision, inputs, extra)
            thinking_mode, prompt_policy = reconcile_controller_with_final_tier(
                thinking_mode,
                prompt_policy,
                extra,
            )

        decision = large_context_floor(
            decision,
            tiers=inputs.tiers,
            valid_tiers=inputs.valid_tiers,
            material_tokens=inputs.material_estimated_tokens,
            context_window_tokens=inputs.context_window_tokens,
            extra=extra,
            metadata_updates=metadata_updates,
        )
        if decision.source == "large_context_floor" and extra is not None:
            thinking_mode, prompt_policy = reconcile_controller_with_final_tier(
                thinking_mode,
                prompt_policy,
                extra,
            )

        # Budget gate runs last: it can only hold or lower the tier, never
        # raise it. With ``budget is None`` (the default) the whole block is
        # skipped, so routing is byte-identical to the pre-gate pipeline.
        if inputs.budget is not None:
            budget_result = budget_gate(
                decision.tier,
                valid_tiers=inputs.valid_tiers,
                budget=inputs.budget,
            )
            decision = apply_budget_gate(
                decision,
                budget_result,
                tiers=inputs.tiers,
                extra=extra,
                metadata_updates=metadata_updates,
            )

        return PolicyResult(
            decision=decision,
            thinking_mode=thinking_mode,
            prompt_policy=prompt_policy,
            metadata_updates=metadata_updates,
        )
```

► **注解**：
- `:948-949` `_finalize` 是 history-aware 路径的核心（下面 3.14.2 讲）。
- `:950-954` `reconcile_controller_with_final_tier`：_finalize 改了 tier 后，controller 的 thinking/prompt 要重新对齐。
- `:956-964` `large_context_floor` 在 `_finalize` **外层**执行：根据 material tokens、context window 和配置阈值，把过低的 tier 抬到 c2/c3。它是**上下文规模下限**，**不是** provider 真正调用前的 token 计费估算。
- `:972-987` budget gate 是**最后一个** policy 阶段，默认关闭（`budget is None` 时整个块跳过）。

### 3.14.2 `_finalize()` 的顺序（完整代码）

源码：`policy.py:996-1073`（关键分支贴出）

```python
    def _finalize(
        self,
        decision: RoutingDecision,
        inputs: PolicyInputs,
        extra: dict,
    ) -> RoutingDecision:
        base_tier = normalize_text_tier(decision.tier) or decision.tier
        final_tier = base_tier
        base_route_class = extra.get("route_class") or route_class_for_tier(base_tier)
        if base_route_class is not None:
            extra["route_class"] = base_route_class
            extra.setdefault("top1_label", base_route_class)

        pre_confidence_tier = final_tier
        gate = confidence_gate(
            final_tier,
            confidence=decision.confidence,
            router_cfg=inputs.router_cfg,
            valid_tiers=inputs.valid_tiers,
            tiers=inputs.tiers,
            calibration=inputs.calibration,
        )
        final_tier = gate.tier

        now = inputs.now if inputs.now is not None else time.monotonic()
        window = float(
            getattr(inputs.router_cfg, "kv_cache_anti_downgrade_window_seconds", 600)
        )
        previous_entry = previous_final_entry(inputs.routing_history, now, window)
        previous_tier = previous_final_tier(previous_entry)
        previous_route_class = None
        if previous_entry:
            previous_route_class = previous_entry.get("final_route_class") or previous_entry.get(
                "route_class"
            )

        complaint = complaint_upgrade(
            final_tier,
            message=inputs.message,
            router_cfg=inputs.router_cfg,
            valid_tiers=inputs.valid_tiers,
            pre_confidence_tier=pre_confidence_tier,
            previous_tier=previous_tier,
        )
        final_tier = complaint.tier

        downgrade = anti_downgrade(
            final_tier,
            router_cfg=inputs.router_cfg,
            valid_tiers=inputs.valid_tiers,
            previous_tier=previous_tier,
        )
        final_tier = downgrade.tier

        gate_capabilities = capability_gate(
            final_tier,
            valid_tiers=inputs.valid_tiers,
            tier_capabilities=inputs.tier_capabilities,
            turn_has_image=inputs.turn_has_image,
            material_tokens=inputs.material_estimated_tokens,
            # ...（后续字段 context_window 等）...
        )
        # ...（bind final_tier / final model）...
        return ...
```

► **注解（_finalize 顺序）**

对当前 history-aware V4 路径，`_finalize()` 的顺序是：

```text
分类 tier (base_tier)
  -> confidence_gate        (:1010-1018)
  -> complaint_upgrade      (:1032-1040)
  -> anti_downgrade         (:1042-1048)
  -> capability_gate        (:1050+)
  -> bind final_tier / final model
```

各 gate 的行号定位：
- `confidence_gate`：`policy.py:213-248`
- `complaint_upgrade`：`policy.py:260-292`
- `anti_downgrade`：`policy.py:301-316`
- `capability_gate`：`policy.py:346-427`
- `large_context_floor`：`policy.py:546-588`（在 run() 里，不在 _finalize）
- `budget_gate`：`policy.py:628-689`（在 run() 里，最后执行）

**各 gate 的语义**：
- **confidence gate**：置信度低于阈值时回到 configured default tier；对高于默认 tier 的候选有 `confidence_high_tier_margin` 折扣。校准开启时会对阈值和 per-class confidence bias 做有界调整。
- **complaint upgrade**：短消息命中配置的 complaint terms 时升级 tier。升级起点取**当前 tier、confidence gate 前 tier、上一轮 tier 三者中的最高者**（`pre_confidence_tier` 传入），避免"先被低置信度降回默认，再被投诉规则错误地只升一级"。
- **anti-downgrade**：在 `kv_cache_anti_downgrade_window_seconds`（默认 600s）内，如果当前结果低于上一轮最终 tier，且功能开启，则保持上一轮 tier。它是 **engine policy 层**规则，和 V4 bundle 内可选 sticky tier（3.13）是**两层不同机制**。
- **capability gate**：**只有** model catalog 给出确定事实时才动作：当前 turn 需要图片但候选模型明确不支持 vision → 沿 canonical ladder 向上找支持 vision 的 tier；material token 超过候选模型明确的 context window → 向上寻找能容纳的 tier；找不到确定能容纳的 tier 时饱和到最高 tier。**能力信息未知时不应假装"不支持"**——gate 的设计是对确定的不足向上走，未知信息不动作。
- **large context floor**：根据 material tokens、context window 和配置阈值，把过低的 tier 抬到 c2/c3。
- **budget gate**：最后一个 policy 阶段，默认关闭。开启后：spend 未知 → suspended 不改路由；预算未超 → no-op；warn → 只记录告警不改 tier；cap → 只能把 tier 降到严格更低的 cap target，**绝不会**因为 budget gate 把 tier 升高。

> **关键结论**："策略引擎总是为了安全升级"**不准确**；能力/上下文门控会升级，预算 cap 则可能**降级**或只告警。

---

## 3.15 最终 metadata 与可观测性

路由结束后，至少应能从 turn metadata / decision record 中区分：

```text
routed_tier              # policy 后最终路由 tier
routed_model             # tier 配置的模型
applied_model            # rollout 后实际应用模型
routing_applied          # 是否真的切换 baseline
routing_source           # v4/image/hold/default 等
routing_confidence       # 分类 confidence
routing_extra            # probabilities、margin、route/policy trail
thinking_mode            # T0-T3
prompt_policy            # P0-P2
router_fallback_chain    # 低 tier fallback
router_budget_*          # budget gate 若动作
provider mismatch fields # provider/continuity/veto 状态
```

`routing_extra` 会保存 raw/model belief 与 policy final decision 的差异。例如 `route_class` 可能是模型后处理结果，`final_route_class` 可能是 confidence、complaint、anti-downgrade 或 capability gate 后的结果。诊断时**不要只看 `routed_tier`**，否则无法知道 tier 是 ML 选出来的还是策略强制的。

---

## 3.16 自学习闭环：明确哪些是 opt-in

相关目录：`src/opensquilla/squilla_router/self_learning/`

可观察的闭环是：

```text
turn decision record
  -> feedback/sample capture（按配置）
  -> offline train
  -> evaluate candidate vs active
  -> gates
  -> promotion / rollback
  -> invalidate_strategy_cache
```

需要避免两个过度推断：
1. 日常 turn**不代表**默认自动重训；capture、self-learning 和 promotion 受配置和后台任务控制。
2. 新模型被训练**不代表**立刻在线；必须经过评估、门控和 active pointer 切换，之后还要**清策略缓存**才能让现有进程重新加载。

V4 inference 在 opt-in capture 时可以把实际使用的 feature vectors 放到 `intermediates`（`core.py:75-78`），供离线 trainer 复用，减少 train/serve skew；这些大型向量会从普通 `routing_extra` 中移出（`apply_squilla_router` 的 `:1282-1285`），避免进入 decision log/history。

---

## 3.17 操作员 hold 与 replay

router-control hold 存放在 `RouterControlHoldStore`，由 TurnRunner 持有并通过 metadata/ToolContext 传给 router 和工具（第 1 章 1.4 讲过）。有效 hold 会在分类前消耗一次（`:1175` `decrement=True`），并以 `source="router_control_hold"` 生成确定性决策，`confidence` 为 1.0（3.6 已贴代码）。

如果工具执行过程中发生改变路由 hold 的操作，结果可能带 `RouterControlReplayEvent`。runtime 会增加 replay depth 重新运行 turn；它会**避免重复持久化**已经处理的用户输入（`persist_input=False`）。调试这类问题时应同时查看 hold action、replay depth 和两次 turn 的 decision record。

---

## 3.18 一个完整样例：为什么最终 tier 不等于 argmax

假设 V4 主融合概率为：

```text
R0=0.06, R1=0.14, R2=0.57, R3=0.23
```

对于一条带有大文件上下文的重构请求：

```text
1. argmax                     -> R2 / c2          (postprocess.py:167)
2. margin upgrade             -> margin 足够大，不升级  (:173-175)
3. aux downgrade              -> 若配置启用且 aux downgrade 足够强，才可能向下  (:178-179)
4. R1 rescue                  -> 通常不影响 R2     (:180)
5. under-routing safety       -> 当前已 >= R2，不动作  (:181)
6. flags/context/sticky       -> long_context 或 sticky 可能改变 controller/route  (:190-194)
7. controller                 -> 派生 T2/T3 与 P1/P2，并做 normalize  (controller.py:76-124)
8. confidence gate            -> 低置信度可能回到 c1   (policy.py:1010-1018)
9. complaint/anti-downgrade   -> 投诉升级或保留上一轮更高 tier  (policy.py:1032-1048)
10. capability gate           -> 明确窗口不足时向上走   (policy.py:1050+)
11. large_context_floor       -> material context 达阈值时至少 c2/c3  (policy.py:956-964)
12. budget gate               -> 超预算且 cap 开启时可能降到指定低 tier  (policy.py:975-987)
13. provider mismatch veto    -> 可选地改到 active provider 可执行 tier  (squilla_router.py:1374-1383)
14. bind                      -> 写 final_tier/final_model  (squilla_router.py:1384-1390)
15. rollout                   -> observe 只记录；full 才真正应用模型  (squilla_router.py:1373)
```

因此**不能**用一组手写概率直接断言最终模型；最终结果还取决于配置、上一轮历史、model catalog、material token、session spend、active provider 和 rollout phase。文档示例应标注"示例输入/中间结果"，**不要**伪装成每次运行的固定输出。

### 3.18.1 三个必须用例验证的路由案例

**案例 A：普通文本，observe**

```text
输入：router enabled=true, rollout_phase=observe, classifier -> c2
预期：
  routed_tier     = c2
  routed_model    = c2 配置模型
  routing_applied = false      ← observe
  applied_model   = baseline model
错误实现：只看 routed_model 就认为 provider 使用了 c2。
```

**案例 B：图片但 caption 为空**

```text
输入：message=" ", attachments=[{mime:"image/png"}], rollout_phase=observe
预期：
  不进入 empty-text return
  进入 image route
  confidence = 1.0
  routing_applied = true       ← 图片强制应用
  applied_model = supports_image tier model
错误实现：先检查 message.strip()，导致图片请求被跳过（这正是 image gate 在 empty-text guard 前的原因）。
```

**案例 C：ML 选择 c1，但 context 明确不足**

```text
输入：classifier -> c1, material_estimated_tokens > c1 的确定 context window, c2 有足够窗口
预期：
  base_tier  = c1
  capability/large-context policy
  final_tier = c2
  routing_extra 记录提升 trail
  controller 与 final tier 重新 reconcile
错误实现：只把 classifier 返回值写入 ctx.model，跳过 policy 和 controller reconcile。
```

---

## 3.19 路由调试顺序

遇到"为什么没有使用预期模型"时，按下面顺序查：
1. `quilla_router.enabled`、`rollout_phase` 和 `tiers` 是否有效。
2. 是否是 `:subagent:`、图片、空文本或 router-control hold 路径。
3. `routing_source`、`routing_confidence`、`routed_tier`、`applied_model` 是否分离。
4. `routing_extra.final_tier`、`routing_trail` 是否被 confidence/complaint/anti-downgrade/capability/context/budget 改写。
5. `routing_applied` 是否为 false（尤其 observe）。
6. tier provider 是否与 active provider 不一致，是否发生 veto/continuity block。
7. cloned selector 最终 resolve 的 provider/model 是什么。
8. router runtime status 是否显示 V4 unavailable、baseline fallback、heuristic 或 default-only。
9. routing timeout 是否触发 pipeline fail-open。
10. 当前 session 的 persisted/in-process routing history 是否导致 anti-downgrade 或 sticky。

---

## 3.20 验收清单

修改路由代码或配置后，至少验证：
- `c0<c1<c2<c3` 的比较不受配置字典顺序影响（`tier_index`）。
- `t0..t3` 兼容归一化不会产生重复 tier。
- 空文本不会调用文本 ML；空 caption 图片仍能走 image route。
- 没有 vision tier 的图片 turn 会产生明确错误（`:1110-1113`），并由 pipeline 的外层策略决定是否继续。
- observe 阶段会记录 would-be route，但**不会**误称为实际模型切换。
- aux 头不会被误当成第三个主分类融合头。
- learned bundle 失败时先回退 baseline，再回退 heuristic/default。
- confidence、anti-downgrade、capability、large-context、budget 的顺序符合 policy engine。
- budget gate 不会意外升级 tier，未知 spend 不会强行 cap。
- provider mismatch 的 route/veto/cross-provider 三种行为可观测且不静默。
- fallback chain 从选定 tier**向更低 tier**构造，且跨 provider 时不会错误复用。
- final metadata 能区分 `base_tier`、`final_tier`、`routed_model` 和 `applied_model`。
- promotion/rollback 后 strategy cache 和 routing history 能正确失效/重载。

---

## 3.21 小结

`SquillaRouter` **不是**一个"ML argmax 后直接换模型"的函数。它是一个带**确定性短路、可选 ML、后处理、历史策略、能力约束、预算约束、provider 约束和 rollout 控制**的决策系统：

```text
输入事实
  -> image/subagent/hold 短路判断       (切片一、二、三)
  -> V4 feature + main/MLP inference    (core.py)
  -> postprocess                        (postprocess.py，9 步)
  -> RoutingPolicyEngine                (policy.py，6 个 gate)
  -> provider mismatch / controller reconcile  (切片六、七)
  -> final_tier + routed_model
  -> rollout phase 决定是否写入 applied_model  (切片六)
```

最重要的工程原则是：**路由必须可解释、可回退、可观测**；任何一次"升级/降级/不应用"都应能从 metadata 和 decision trail 中找到原因，**而不是**只能重新运行模型猜测结果。

---

> **版本与准确性说明**：本章基于 OpenSquilla 当前源码（HEAD `097db9d3`）实测行号。`apply_squilla_router` 本身约 420 行（`engine/steps/squilla_router.py:1066-1487`），本章按 7 个逻辑阶段切片完整呈现了关键部分；postprocess（157-213）、policy.run（941-994）、policy._finalize（996-1073）、inference core.predict（63-87）均完整贴出。代码会持续演进，若发现文档与代码不符，以代码为准。
