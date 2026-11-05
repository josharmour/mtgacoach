# Using MTGA Coach 4.0

Keep the compact Coach window beside Arena. Start at **Your next decision**; open the supporting detail when you want to understand the recommendation. The coaching area scrolls in a shorter window while autoplay and speech controls stay available at the bottom.

## Read the screen

| Part of the screen | What it tells you | How to use it |
| --- | --- | --- |
| Turn strip and connection chips | Whose turn it is, the phase, and Coach/Arena connection status. | Check that Coach is following this game. The decision card separately identifies your response window; the active player is not always the player with priority. |
| Your next decision | The current action or waiting state, a short reason, and a threat to watch. | Start here before reading a full plan. A completed action in history is not a new instruction. |
| Outlook | A balance scale and an ahead/even/behind assessment, with contributing factors. | See how the position is changing. This is a board heuristic, not a percentage chance of winning. The scale moves when the assessment changes. |
| Paths from here | The leading searched line and up to two alternatives. | Follow the turn markers to compare possible continuations. Expand a path for its steps and projected life totals. Inspecting a path does not select moves for autoplay. |
| Forecast assumptions | What the search can and cannot account for. | Read this before relying on a distant outcome. Solid segments show searched continuations; dashed segments show approximate continuations. Future draws and unknown opponent spells are not modeled, and the third turn uses a greedy continuation. |
| Plan & advice details | Longer coaching explanations and available plan details. | Expand when you want the reasoning behind the short recommendation. |
| Life summary; Hand & board | Life totals and the game state observed by Coach. | Check the position the advice is based on. Floating mana is shown only when explicitly known. |
| Activity history | Recent advice and events. | Review what happened without mixing past actions into the current decision. |
| Rank & recent matches | Constructed/Limited rank and access to recent ranked results. | Expand between games and open the relevant ladder’s history. |

### What the green line means

A green glow identifies the **leading supported candidate found for the current board**. It does not establish that the game is won or that all possible lines were searched. A survival line can lead while the overall outlook is Behind, and the best modeled line can still project a loss. Read the outcome beneath the path.

When the observed board changes, Coach removes the old leading highlight and marks the outlook **Reassessing** until a matching assessment arrives. Previous paths may remain as labeled context. A truncated search or an unsupported or unknown forecast does not earn the green leading highlight.

To stop the scale moving, expand **Help & settings** and turn off **Animate outlook changes**. High-contrast mode also uses a static scale. Color is accompanied by text labels and outcomes.

## Choose what you hear

Open **Speech & voices** at the bottom of the window, or **Tools → Speech & voices…**.

- **Hear → Coach advice** speaks coaching suggestions.
- **Hear → Announcers narrate the game** uses a play-by-play voice and an analyst. **Hype** adds playful reactions and rising energy at big moments; **Technical** favors measured delivery and strategic explanation. Voice selection is separate from broadcast style.
- **Voice engine → Kokoro or Chatterbox** chooses the speech provider. The desktop Chatterbox choice uses the hosted service with your Coach sign-in; it needs no local model installation. Available voices come from that service.
- **Preview** auditions the selected voice or both announcers without saving. Adjust speed and the voice or pair, then save your choices. **Discuss my hand and deck** controls whether announcer context includes that private game information.

**Stop speech** interrupts playback; it does not stop autoplay. Turn speech off in the speech dialog to keep subsequent advice or narration silent. If hosted Chatterbox is unavailable, the app reports it; retry Preview or choose Kokoro.

Managed packages include the ArenaOnAir core used for generated commentary. Source users need the [additional core setup](commentary.md#runtime-requirement). Commentary and coaching remain model-generated and can miss an interaction; use the observed game and the displayed assumptions to judge the advice.

## Control play and the next match

**Enable autoplay / Pause autoplay** controls automated play in the current match. It requires a working platform bridge as well as Coach access. You can leave autoplay off and use the same visual and spoken advice while playing manually.

**After match…** holds **Auto Queue** and **Grind**. Auto Queue requests another game with the most recently played queue and deck; Grind configures a timed session for quests and mastery XP. These controls require autoplay. Check them before walking away if you only intend to play one game.

## Recover, update, or report a problem

Expand **Help & settings** for **Autopilot bug**, **Debug report**, **Contact Josh**, **Reload engine**, **Restart Coach**, and the update control when available. **Reload engine** restarts the coaching process while keeping the app open; **Restart Coach** reloads the desktop app as well. Finish the match before applying an update.

Use **Tools → Setup & Repair** for installation, detailed-log, access, and bridge checks. For a bridge replacement, follow the platform instructions and close Arena first. [Setup and migration](../INSTALL.md) · [Update behavior](../RELEASE.md#what-happens-after-installation)

In a source **Dev** window, **Copy commit** copies the full revision captured when Coach started. Include it when reporting a source-build issue; changing the checkout does not change the code already running in the process.
