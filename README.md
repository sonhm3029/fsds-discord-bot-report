# fsds-discord-bot-report

Bot Discord chạy liên tục trên server Linux cá nhân, tự đồng bộ **báo cáo nghiên cứu + toàn bộ thảo luận trong thread** từ Discord sang **Google Docs**, mỗi thread là một **tab** trong Doc của sinh viên tương ứng. Nhờ vậy ChatGPT đọc được qua Google Drive connector mà bạn không phải copy tay.

```
Discord channel của Sophie ──┐                       Google Drive/KV-Cache Optimization/Sophie (Doc)
  ├─ thread "Week 1 ..."  ───┼──► bot ──► Docs API ──►  ├─ Tab: Report 2026-10-05 - Week 1 ...
  └─ thread "Week 2 ..."  ───┘                          └─ Tab: Report 2026-10-12 - Week 2 ...
```

## Cách hoạt động

- **Một thread = một tab.** Report là tin nhắn mở thread (text channel) hoặc bài đăng của forum channel. Nhiều học viên có thể dùng chung một channel (ví dụ mỗi topic một channel): thread thuộc về học viên **viết tin nhắn report gốc**, nhận diện bằng `users` (Discord user id) trong config. Thread do người khác mở (ví dụ thread của thầy/cô) bị bỏ qua. Nếu thread không có tin gốc, bot dùng tác giả của tin đầu tiên. Bot lưu nguyên văn *toàn bộ* tin nhắn của cả hai phía, theo thứ tự thời gian: tác giả, giờ, link "open in Discord", id tin nhắn, reply (↪), code block, link, file đính kèm, embed.
- **Idempotent.** Mỗi lần có thay đổi, bot đọc lại *cả thread* từ Discord, dựng lại nội dung tab, và chỉ ghi lên Google khi nội dung (hash) khác lần trước. Vì thế: tin nhắn sửa/xóa được phản ánh, restart không tạo trùng, bỏ lỡ event cũng tự đúng.
- **Không bỏ sót.** Ba lớp: (1) event của Discord (tin nhắn mới/sửa/xóa, thread mới/đổi tên) → chờ `debounce_seconds` rồi sync; (2) quét định kỳ `poll_interval_seconds` so sánh Discord với DB; (3) khi khởi động quét toàn bộ thread (cả archived) và cứ `full_resync_hours` giờ đọc lại hết để bắt sửa/xóa lúc bot offline.
- **Trạng thái** lưu ở SQLite (`data/state.db`): `thread_id → doc, tab`. Nếu mất file này, bot tự nhận lại các tab cũ nhờ dòng `thread_id: …` ở đầu mỗi tab, không tạo trùng.
- Tab đầu tiên rỗng ("Tab 1") của một Doc mới được tái sử dụng cho report đầu tiên. Tab có nội dung bạn tự viết thì bot không đụng tới.

## Cần chuẩn bị (checklist)

| Thứ | Lấy ở đâu |
|---|---|
| `DISCORD_TOKEN` | Bước 1 |
| ID các channel và user id của từng học viên | Bước 1 |
| File key service account JSON | Bước 2 |
| URL các Google Doc (bạn tạo sẵn) đã share cho service account | Bước 2 |

## Bước 1 — Discord bot

1. <https://discord.com/developers/applications> → **New Application** → tab **Bot** → **Reset Token** → copy token (đây là `DISCORD_TOKEN`).
2. Cũng ở tab **Bot**, bật **Message Content Intent** (Privileged Gateway Intents). Thiếu mục này bot sẽ không đọc được nội dung tin nhắn.
3. Mời bot vào server bằng link (thay `APP_ID` bằng *Application ID* ở tab General Information):

   ```
   https://discord.com/oauth2/authorize?client_id=APP_ID&scope=bot&permissions=66560
   ```
   `66560` = View Channels + Read Message History. Bot cần thấy các channel của sinh viên (kiểm tra channel không chặn quyền của bot). Muốn đọc cả **private thread** và liệt kê private thread đã archive, cấp thêm **Manage Threads**: dùng `permissions=17179935744`.
4. Bật *User Settings → Advanced → Developer Mode*, chuột phải từng channel → **Copy Channel ID**; chuột phải từng học viên → **Copy User ID**. Chuột phải vào *category* sẽ cho ID của category, không dùng được.
5. Channel bị khóa (private) thì cấp cho role của bot quyền **View Channel** + **Read Message History** ở channel hoặc category.

Channel có thể là text channel (thread tạo từ tin nhắn) hoặc forum channel. Tin nhắn nằm ngoài thread không được đồng bộ.

## Bước 2 — Google Docs (service account)

1. <https://console.cloud.google.com/> → tạo project → **APIs & Services → Library → Google Docs API → Enable**. (Không cần bật Drive API.)
2. **IAM & Admin → Service Accounts → Create** (không cần gán role) → mở service account → **Keys → Add key → JSON** → tải file về, đặt vào `secrets/google-credentials.json` trên server.
3. Tạo Google Doc cho từng sinh viên trong đúng folder topic như bạn muốn, rồi **Share** từng Doc cho email của service account (dạng `xxx@project.iam.gserviceaccount.com`, nằm trong file JSON ở trường `client_email`) với quyền **Editor**.
4. Copy URL của Doc, dán vào `config.toml`.

> Mẹo: share **cả folder** topic (hoặc folder cha) cho service account với quyền Editor, mọi Doc trong đó, kể cả Doc tạo sau này, tự có quyền, khỏi share từng Doc.

> Nếu tổ chức Google Workspace của bạn chặn tạo key cho service account (`iam.disableServiceAccountKeyCreation`) hoặc chặn share ra ngoài, dùng OAuth: xem phần *Phương án OAuth* bên dưới.

## Bước 3 — Cài đặt trên server

Cần Python ≥ 3.11.

```bash
sudo mkdir -p /opt/discord-bot-report && sudo chown $USER /opt/discord-bot-report
git clone https://github.com/sonhm3029/fsds-discord-bot-report /opt/discord-bot-report
cd /opt/discord-bot-report

python3 -m venv .venv
.venv/bin/pip install -e .

cp config.example.toml config.toml        # sửa: student, doc, channels, timezone
cp .env.example .env && chmod 600 .env    # điền DISCORD_TOKEN
mkdir -p secrets && chmod 700 secrets     # đặt google-credentials.json vào đây
chmod 600 secrets/google-credentials.json
```

Sửa `config.toml`: mỗi sinh viên một khối `[[students]]` với `name`, `doc` (URL hoặc ID), `channels` (danh sách id), `users` (Discord user id của học viên, cần khi nhiều học viên chung channel). Xem chú thích trong [config.example.toml](config.example.toml).

## Bước 4 — Kiểm tra rồi chạy

```bash
.venv/bin/python -m discord_gdocs_sync check       # xác minh quyền Google Doc + Discord channel
.venv/bin/python -m discord_gdocs_sync sync-once   # đồng bộ hết một lượt rồi thoát (khuyến nghị chạy thử trên 1 Doc trước)
.venv/bin/python -m discord_gdocs_sync run         # chạy liên tục
```

Lần đầu (backfill) mỗi thread tốn khoảng 5–10 giây vì Docs API chỉ cho 60 lần ghi/phút; ~100 thread mất cỡ 10–15 phút. Bot tự giãn nhịp và tự retry khi bị giới hạn.

## Bước 5 — Chạy nền bằng systemd

Sửa `User` và đường dẫn trong [deploy/discord-gdocs-sync.service](deploy/discord-gdocs-sync.service), rồi:

```bash
sudo cp deploy/discord-gdocs-sync.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now discord-gdocs-sync
journalctl -u discord-gdocs-sync -f        # xem log
```

Cập nhật phiên bản mới: `git pull && .venv/bin/pip install -e . && sudo systemctl restart discord-gdocs-sync`.

## Dùng với ChatGPT

Sau khi bot chạy, hỏi ChatGPT như "Đọc report mới nhất của Sophie trong Google Drive", "Tìm report mà mình bàn về giả thuyết KV-cache". Tên tab mặc định là `Report {ngày học viên đăng report} - {tên thread}` (đổi bằng `tab_title_format`, tối đa 50 ký tự theo giới hạn của Google); tên thread càng rõ nghĩa thì ChatGPT càng dễ tìm. Các tab luôn xếp theo thứ tự thời gian đăng, kể cả khi bot đồng bộ muộn một thread cũ. Mỗi tab có header chứa sinh viên, topic, link thread và id.

## Hành vi cần biết

- **Sửa tin nhắn** → nội dung tab cập nhật (có ghi `edited …`). **Xóa tin nhắn** → tin nhắn biến mất khỏi tab (tab luôn phản ánh trạng thái hiện tại của Discord).
- **Xóa/archive thread** → tab được giữ nguyên. Đổi tên thread → đổi tên tab.
- **Đừng sửa tay nội dung tab do bot tạo**: lần thay đổi kế tiếp của thread sẽ ghi đè. Muốn ghi chú riêng thì tạo tab riêng.
- **File đính kèm văn bản nhỏ** (`.md`, `.txt`, `.csv`, `.json`, mã nguồn… dưới 256 KB) được tải về và **nhúng nội dung vào tab** (file `.md` được dựng như Markdown), vì nhiều báo cáo là file `.md`. **Ảnh** PNG/JPEG/GIF được chèn thẳng vào Doc. **File khác** (pdf, ipynb, file lớn…) chỉ có link CDN của Discord, và link này *hết hạn sau khoảng 24 giờ*; link "open in Discord" ở mỗi tin nhắn vẫn dùng được. Server chạy bot cần ra được `cdn.discordapp.com`; nếu không tải được, bot vẫn đồng bộ phần còn lại và tự bổ sung nội dung file ở lần đồng bộ sau.
- Tin nhắn dạng poll, sticker, reaction không được lưu.

## Xử lý sự cố

| Triệu chứng | Nguyên nhân thường gặp |
|---|---|
| `Enable 'Message Content Intent'…` | Chưa bật intent ở Bước 1.2 |
| `check` báo `403`/`404` ở Google Doc | Chưa share Doc cho service account với quyền Editor, hoặc chưa bật Google Docs API |
| `channel … not found or not visible` | Sai channel ID hoặc bot chưa được mời / bị chặn quyền xem channel |
| Thread private không được sync | Cấp thêm **Manage Threads** cho bot, hoặc thêm bot vào thread |
| Log `Skipping thread … not mapped to a student` | Tác giả tin nhắn report chưa có trong `users` của học viên nào. Thêm user id vào config nếu đó là học viên |
| Log `Google Docs HTTP 429; retrying` | Chạm quota 60 ghi/phút. Bình thường khi backfill, bot tự chờ |
| Có tab trùng sau khi xóa `state.db` | Không nên xảy ra (có cơ chế nhận lại tab). Nếu có, xóa tab thừa; thread sẽ được ghi vào tab còn lại |

## Phương án OAuth (thay cho service account)

Dùng khi tổ chức chặn tạo key cho service account (`iam.disableServiceAccountKeyCreation`). Bot sẽ chạy bằng quyền của chính tài khoản Google của bạn nên **không cần share Doc cho ai**, miễn là tài khoản đó sửa được các Doc.

1. Google Cloud Console (cùng project đã bật Google Docs API) → **Google Auth platform** (hoặc *APIs & Services → OAuth consent screen*) → **Get started**. Chọn User type **Internal** nếu project thuộc một tổ chức Workspace và Doc nằm trong tài khoản Workspace đó: không cần xác minh app, token không bị hết hạn. Nếu chỉ chọn được **External** thì sau khi tạo phải bấm **Publish app** (*In production*); để *Testing* thì Google hủy refresh token sau 7 ngày.
2. **Clients → Create client → Application type: Desktop app** → tải file JSON về (ví dụ `client_secret.json`).
3. Ở máy có trình duyệt:
   ```bash
   .venv/bin/pip install -e ".[oauth]"
   .venv/bin/python scripts/oauth_login.py client_secret.json secrets/google-credentials.json
   ```
   Nếu chạy trên server không có trình duyệt (SSH): thêm `--port 8765 --no-browser`, forward cổng 8765 về máy của bạn (`ssh -L 8765:localhost:8765 user@server`, hoặc panel *Ports* của VS Code), rồi mở URL script in ra trong trình duyệt trên máy bạn.
   Trình duyệt mở ra, đăng nhập đúng tài khoản chứa các Doc và cho phép quyền Google Docs. Màn hình "Google hasn't verified this app" là bình thường với app của chính bạn: chọn *Advanced → Go to … (unsafe)*.
4. Nếu chạy bot trên máy khác, copy `secrets/google-credentials.json` sang (giữ quyền `chmod 600`). `client_secret.json` sau đó không cần nữa.

Cách này vẫn dùng chung config: `credentials_file` trỏ tới file vừa tạo.

## Phát triển

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

Cấu trúc: [render.py](src/discord_gdocs_sync/render.py) (thread → text + style, index UTF-16), [gdocs.py](src/discord_gdocs_sync/gdocs.py) (REST client + dựng batchUpdate), [sync.py](src/discord_gdocs_sync/sync.py) (engine idempotent), [discord_source.py](src/discord_gdocs_sync/discord_source.py) (discord.py → snapshot), [bot.py](src/discord_gdocs_sync/bot.py) (event, debounce, quét định kỳ), [state.py](src/discord_gdocs_sync/state.py) (SQLite).
