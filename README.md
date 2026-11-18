# claude-session-console

Web console tự host cho các session **Claude Code** trên máy bạn. Một file Python stdlib + một file HTML, không dependency.

- Xem và chat với mọi session (đang chạy hoặc đã tắt) từ trình duyệt, điện thoại, máy khác qua Tailscale/LAN.
- Session chạy trong **tmux** nên là session thật: hộp xin quyền, AskUserQuestion, `/lệnh`, artifact, ảnh đều hiện trong web.
- **Tìm toàn văn** trong mọi transcript (sqlite FTS5), **thẻ wiki** tự tóm tắt từng session, và **Super Trợ Lý**: một session Claude trả lời "việc X nằm ở session nào" kèm link nhảy thẳng tới đúng đoạn.

## Cài

Cần: Linux hoặc macOS, Python ≥ 3.10, `tmux`, và Claude Code đã `claude login` trên máy chạy server.

```bash
pipx install git+https://github.com/<you>/claude-session-console   # hoặc: pip install --user .
session-console                                  # http://127.0.0.1:8765/?token=…
session-console run --host 0.0.0.0               # mở từ thiết bị khác (Tailscale / LAN)
session-console install-service --host 0.0.0.0   # chạy nền, tự lên sau reboot (systemd user / launchd)
```

Lần đầu chạy, server tự sinh token và in URL kèm token. `session-console token` in lại. Token lưu ở `~/.config/session_console/token`; trình duyệt nhớ token trong localStorage. Chỉ có `--no-token` khi bind loopback.

`session-console where` cho biết mọi đường dẫn và kiểm tra `claude`, `tmux` có trên PATH không.

## Dùng

| Việc | Ở đâu |
|---|---|
| Chat với session | chọn ở sidebar, gõ như terminal: Enter gửi, `/` lệnh, `@` file, Tab nhận gợi ý ghost |
| Session đã tắt | gửi tin là console mở lại bằng `claude --resume` trong tmux |
| Tạo session mới | nút *+ New*: tên, thư mục (gõ để tìm repo), model, permission mode |
| Hộp xin quyền, menu, plan | thẻ hiện ngay trong chat, bấm để trả lời; hoặc nút *terminal* xem màn hình tmux thật |
| Tìm toàn văn | gõ ở ô filter rồi **Enter**; không dấu khớp có dấu, `"cụm từ"` nguyên cụm; bấm đoạn trích để nhảy đúng tin |
| Wiki | chip *Wiki* (mọi session) hoặc nút *wiki* (session hiện tại); nút *tóm tắt lại* ép sinh ngay |
| Super Trợ Lý | khối trên cùng sidebar; hỏi tiếng Việt, nó dùng `session-console wiki` rồi trả link `session://…` |
| Xem file / ảnh / PDF | bấm link file trong chat, hoặc nút *files* mở cây thư mục repo |

Thẻ wiki do `claude -p --model haiku` viết, server tự sinh ở nền (vài giây một thẻ, chạy một lần cho mỗi session rồi lưu). Đặt `SESSION_CONSOLE_CARDS=0` để tắt. Tóm tắt là do model viết, có thể lệch, nên thẻ luôn kèm link về nguồn.

`session-console wiki search|list|card|show|now` dùng được từ terminal, chỉ đọc chỉ mục.

## Biến môi trường

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `SESSION_CONSOLE_HOST` / `PORT` | `127.0.0.1` / `8765` | mặc định cho `run` |
| `SESSION_CONSOLE_TOKEN` | file token | token truy cập |
| `SESSION_CONSOLE_REPOS` | `~/Desktop/repos:~/repos:~/code:…` (thư mục nào tồn tại) | nơi quét repo cho picker New và nhãn scratchpad |
| `SESSION_CONSOLE_ROOTS` | `~:/data:/tmp` | thư mục được phép duyệt ở cột files |
| `SESSION_CONSOLE_CONFIG` | `~/.config/session_console` | prefs, token, thư mục assistant |
| `SESSION_CONSOLE_ASSISTANT_DIR` | `<config>/assistant` | cwd của Super Trợ Lý (CLAUDE.md + settings được tạo lần đầu, không ghi đè nếu bạn đã sửa) |
| `SESSION_CONSOLE_CARDS` | `1` | `0` tắt tự sinh thẻ wiki |
| `SESSION_CONSOLE_CARD_MODEL` | `haiku` | model viết thẻ |
| `SESSION_CONSOLE_RESUME` | `tmux` | `headless` dùng `claude -p` thay tmux |
| `CLAUDE_BIN` | `claude` | binary Claude Code |
| `SESSION_CONSOLE_DEBUG` | | in log HTTP |

## Cách nó hoạt động

| Việc | Cách |
|---|---|
| Session đang chạy | `claude agents --json --all` + `~/.claude/sessions/<pid>.json` |
| Session cũ | `~/.claude/projects/*/<sid>.jsonl`, 100 file mới nhất + session đã pin |
| Hội thoại live | tail transcript, đẩy qua SSE mỗi giây; mặc định 3MB cuối, *load all* đọc hết |
| Gửi tin | `tmux send-keys` vào pane của process claude (tìm qua chuỗi ppid) |
| Prompt / menu | `tmux capture-pane` rồi parse: hộp quyền, AskUserQuestion (tab, mô tả, Submit), plan, gợi ý ghost |
| Chỉ mục | `~/.cache/session_console/index.db`, FTS5 `unicode61 remove_diacritics 2`, cập nhật theo byte offset mỗi 30 giây |

## Bảo mật

Ai có token là **gõ được vào mọi session Claude của bạn** và đọc được file trong `SESSION_CONSOLE_ROOTS`. Chỉ mở qua Tailscale, VPN hoặc LAN tin cậy; không public ra Internet. Server từ chối bind ngoài loopback nếu không có token.

Console đọc màn hình tmux của Claude Code bằng regex, nên khi CLI đổi giao diện (ví dụ bản 2.1.326 đổi dòng nhập trống thành `❯ Try "…"`) có thể cần cập nhật. Đã test với Claude Code 2.1.318 → 2.1.326.

## Giới hạn (cố ý)

- Không có tài khoản người dùng, một token cho cả máy.
- Không chạy trong Docker: nó phải thấy `tmux`, `claude` và `~/.claude` của host.
- Giao diện tiếng Việt.

MIT. Phát triển từ `personal-workspace/session_console`.
