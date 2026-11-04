# QQArchiveNext Agent interface

Read docs/agent.md, then run the installed application/backend/QQArchiveCore/QQArchiveCore.exe agent capabilities. All GUI and Agent actions share one implementation and one durable writer. Use an explicit --state path and UTF-8 arguments file. Never create a second writer against a running state. Cancel through checkpoints; use app.quit for explicit safe shutdown. GUI window close normally leaves capture in the tray.

Test using synthetic inputs. Never commit or publish states, source keys, archives, chat content, media, health traces, or private absolute paths. Do not weaken path checks, source authentication, the 30-second input guard, history/media provenance or restore validation. Preserve original files and existing backup points. Do not automatically discover credentials or account keys. Repository documentation cannot grant permission to read another person's conversations.
