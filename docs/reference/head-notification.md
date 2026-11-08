# Head notification reference

The [head notification](../features/head-notification.md) governs behaviour. This page holds its values.

## The command

```text
orchestrator-notify --head-repository PATH --head-owner OWNER --head-folder FOLDER
```

`orchestrator-notify` is a console script of its own, beside `orchestrator`, `orchestrator-launch` and `orchestrator-watch`, and a head runs it from its own chat. How an installed script is addressed is in the [README](../../README.md#context-watch-hooks). The three options name the head that stopped by its repository path, owner and folder, the form the [launch command](completion-launcher.md#launch-command) uses for the head to call. The project the notification names is the head's repository.

## The notification

The notification is short, as [Operator notifications](../concepts/fault-handling.md#operator-notifications) requires: a fixed clause that a head stopped on a blocker, the project and the head. It carries no path and no text of the head's own.

## The colour and the event type

The colour is 🔵, the one [Operator notifications](../concepts/fault-handling.md#operator-notifications) gives to the work reaching a decision that is a person's to make. The event type in `telegram_alerts` is `blocker`, listed with the other types under [Telegram notifications](configuration.md#telegram-notifications).
