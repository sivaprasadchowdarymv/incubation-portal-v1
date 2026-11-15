# What's new in this version (and how to use it)

## For students
| Feature | Where | How it works |
|---|---|---|
| **More than one problem statement** | 👥 My Team → *➕ Work on another problem statement* | Same team, same account. Add another problem statement from the same or another track. Each one has its **own 3 attempts** and its **own result**. The admin sets the limit (default 2). |
| Choose which one to submit | 📤 Submit → *Submit for which problem statement?* | The attempts, draft check and result are counted separately for each problem statement. |
| **Ask the Incubation Team** (replaces the Policy Assistant) | 📨 Ask the Incubation Team | Write a query → it is emailed to the incubation team → you get an email confirmation with a number (Q-0007) → the reply comes by email and appears here. You can add follow-up messages. |
| **Completion acknowledgements** | email | Besides the existing receipts, you get *"Your Phase 1 submission is complete"* when **all** your problem statements are accepted. |
| Results per problem statement | 🏅 Results | One result card for each problem statement. |

## For reviewers
| Feature | How it works |
|---|---|
| Acknowledgements | Emails when: the confidentiality pledge is recorded · an assignment is accepted · a review is submitted · **all assigned reviews are complete** ("thank you") |
| 📨 Ask the Incubation Team | Same query page as students |

## For the admin
| Feature | Where | How it works |
|---|---|---|
| **CMT-style dashboard** | 📊 Dashboard | Filters (track, problem statement, stage, decision, search). Tabs: **By track** (CMT submission summary: teams, entries, stages, reviewers, review completion %, average score, decisions), **By problem statement**, **All entries**, System health. Every table exports to CSV. |
| **Assign by track → problem statement** | 🎯 Peer Review → 2 · Assign | Pick a track (shows how many need reviewers) → a problem statement (shows unassigned count) → the unassigned abstracts → reviewers (experts and least-loaded listed first) → due date → **Assign**. Several abstracts at once. |
| **Queries inbox** | 📬 Queries (shows the number open) | Filter, read the thread, **✨ Draft a reply with AI** (from your knowledge base; you edit it), **Send reply** / **Send and close**. Optional inbox address: ⚙️ Programme Setup → Emails & reminders → *Incubation inbox*. |
| **Edit / remove users** | 🔑 Users | Search and filter. Change email, role, active (deactivate = cannot sign in; open review assignments are withdrawn), reset password. **Delete** only for accounts with no history; others are deactivated so records keep their author. The user is emailed about every change (never the password). |
| **Several AI keys** | ⚙️ Programme Setup → **AI keys** | Paste keys from Groq, Google Gemini, OpenRouter, Hugging Face, Mistral, Cerebras or any OpenAI-compatible service. They are used **in order**; if one is busy or fails, the next takes over and the failing one rests for a minute. **🧪 Test**, ⬆/⬇ order, ⏸ pause, 🗑 remove. Keys show only their last 4 characters. |
| Problem statements per team | ⚙️ Programme Setup → Deadline & rules | 1 to 5 (default 2) |
| Staff AI assistant | 🤖 AI Assistant | The old Policy Assistant, now for staff only |

Existing data is upgraded automatically on the first start (database schema v6): teams that already locked a problem statement keep it as their entry 1.
