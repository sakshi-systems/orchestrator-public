# Courier

## What it is

The courier is the orchestrator's one peer send. It takes a head's identity — repository, owner and folder — and a text, and attempts to deliver the text to that head's live session. Who the head is, is the caller's to resolve; the courier finds it. Its callers are the [completion waiter](completion-launcher.md#evidence-and-delivery) and the [context watch](context-watch.md).

## Finding the head

The courier reads the head's locator fresh at send time, from `<repository>/.ai-factory/architects/<owner>/<folder>/` in the local checkout that commits write, resolving symlinks to the checkout they point at. What a locator is, which of its forms is read and what its fields mean are stated in [Identity, locator and recovery](https://github.com/sakshi-systems/skills-public/blob/main/docs/architect-runtimes.md#identity-locator-and-recovery).

A missing, unreadable, malformed or incomplete address, one without a `runtime` and one naming a runtime the courier does not know are each a logged non-send that names the owner. The courier neither infers a runtime from another field nor repairs a peer's locator.

## Dispatch

The courier chooses its native send once from the fresh address's `runtime`. The recipient-owned [reachability sections](https://github.com/sakshi-systems/skills-public/blob/main/docs/architect-runtimes.md#reaching-a-known-local-head) hold the calling recipes:

- [Codex — How a head of this runtime is reached](https://github.com/sakshi-systems/skills-public/blob/main/src/skills/architect-runtime-codex/SKILL.md#how-a-head-of-this-runtime-is-reached), using the address's `session-id`.
- [Claude — How a head of this runtime is reached](https://github.com/sakshi-systems/skills-public/blob/main/src/skills/architect-runtime-claude/SKILL.md#how-a-head-of-this-runtime-is-reached), using the address's `session-name`, its published permission mode where it has one, and the constrained one-shot send when the sender has no native messaging tool.

Both branches request the supplied text verbatim as the message body. The recipient's published permission mode is passed as the recipe states: an absent mode leaves the one-shot send's own default, and a present mode the recipe does not recognize is a logged non-send, never a silent default. Delivery evidence has the meaning defined by [Architect runtimes](https://github.com/sakshi-systems/skills-public/blob/main/docs/architect-runtimes.md#reaching-a-known-local-head), rather than proving the recipient read or answered.

## Delivery

One attempt is made under the [courier delivery bound](../reference/courier.md#courier-delivery). A failure is logged, with no retry, and a send lost to a rate limit is lost. A send is attempted, never guaranteed, and never changes the outcome of what it reports, as [fault handling](../concepts/fault-handling.md) states for notifications.
