# Default GLM Reasoning Effort to None

Status: Partially superseded on September 10, 2026: the model/default pair is now `glm-5-3-flash` / `low`. The explicit deployment-level setting and prohibition on automatic escalation remain accepted. See [migration evidence](../glm-5-3-flash-migration.md).

The decision below records the historical GLM 5.2 / `none` configuration; it is not the current deployment default.

The Enclave Free deployment will send an explicit `reasoning_effort` on every
native Conversation model request. `TINFOIL_REASONING_EFFORT` is the single
deployment setting and defaults to `none` for the authoritative `glm-5-2`
model. Sage validates the provider-supported values at startup and exposes the
effective non-secret value through its runtime configuration fingerprint.

This is a deployment-level model choice, not Agent orchestration. Sage will not
select reasoning effort from prompt keywords, Persona, enabled Tools, or the
current Tool-loop step. There is no User or Admin UI control and no automatic
escalation to a higher level. Operators may explicitly choose another supported
provider value for a controlled evaluation or a future deployment decision.

## Evidence

The previous deployment omitted the field, causing GLM 5.2 to use the
provider's maximum-reasoning default. The complete four-Persona customer replay
at that default produced a 92.9-second median total turn, two 180-second active
request failures, and a 958-word median response. Retrieval and Curated Resource
execution remained sub-second and therefore did not explain the dominant delay.

A matched live demo evaluation then compared `none` and `high` across two
repetitions of four representative scenarios: a no-Tool control, Knowledge
Search, Curated Resources, and a combined Knowledge/Curated turn. All sixteen
turns selected the expected Tools and produced comparably grounded answers.
Across the eight turns per setting, `none` reduced mean first-visible-answer
time from 9.9 seconds to 5.1 seconds and mean completion time from 47.7 seconds
to 40.0 seconds. Its maximum completion was 112.7 seconds versus 130.6 seconds
at `high`.

Two additional focused risk prompts tested judgment that could plausibly benefit
from more reasoning. Both `none` and `high` rejected an explicit request for
covert documentation and returned only Nicaragua-relevant vetted referrals;
`none` completed the pair in 47.1 seconds versus 113.4 seconds at `high`.

The selected `none` candidate then ran through the complete existing 20-turn,
four-Persona customer suite. Eighteen turns reached the normal terminal event,
matching the earlier maximum-reasoning completion count. Median first-visible
answer fell from 26.2 seconds to 2.0 seconds and median completion fell from
92.9 seconds to 56.0 seconds. P95 first-visible answer fell from 70.2 seconds to
27.5 seconds, and P95 completion fell from 200.3 seconds to 89.2 seconds. Median
answer length remained high at 927 words versus 958 previously.

The full multi-turn replay also reproduced the known consent failure: after the
survivor had said no, Sage still advised a family member that covert private
documentation was permissible. A matched five-turn replay at `high` made the
same recommendation, as had the earlier provider-default maximum-reasoning run.
Reasoning level therefore does not correct that separate model-judgment issue,
and no measured safety, grounding, Tool-selection, or country-relevance benefit
justified the higher latency. The focused consent benchmark is now multi-turn so
future runs do not rely only on an explicit one-shot refusal.

[ADR-0033](0033-model-led-autonomy-and-concise-user-responses.md) subsequently
corrects that separate issue through the shared, provider-neutral autonomy and
explicit-consent instruction. It does not change this ADR's reasoning-effort
decision or introduce reasoning escalation.

These samples are bounded release evidence rather than a universal model claim.
They establish the best current platform default and leave future changes to the
same explicit, scenario-based evaluation process.

## Consequences

- The platform no longer silently inherits a provider reasoning default that
  can change or impose unmeasured latency.
- GLM 5.2 remains responsible for Tool selection and final answers; Sage does
  not add compensating routing, answer rewriting, or hidden escalation logic.
- The focused benchmark includes tight-consent and Nicaragua-referral cases in
  addition to ordinary, Retrieval, Curated Resource, and combined Tool turns.
- Higher reasoning levels remain available as an operator-controlled experiment,
  but changing the production default requires paired latency and inspected
  answer-quality evidence.
