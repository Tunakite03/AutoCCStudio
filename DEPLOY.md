# Deploy tự động lên VM Azure

Mỗi lần push lên `main`, GitHub Actions chạy test → build Docker image → đẩy lên
GHCR → SSH vào VM pull image mới và restart container. Nếu test đỏ thì không có
gì được deploy.

VM không giữ source code và không build gì cả, nó chỉ pull một image đã dựng
sẵn. Đây là lý do chọn cách này: build `faster-whisper` + `av` trên 2 vCPU mất
vài phút và ăn RAM, còn pull image mất vài chục giây.

Luồng đầy đủ nằm trong [ci.yml](.github/workflows/ci.yml); file compose VM dùng
là [docker-compose.prod.yml](docker-compose.prod.yml) (bản
[docker-compose.yml](docker-compose.yml) vẫn dành cho máy dev, có `build:`).

---

## 1. Chuẩn bị trên VM (làm một lần)

Cấu trúc thư mục VM cần có — mặc định workflow dùng `/opt/autocc`:

```
/opt/autocc/
├── .env                       # bạn tự tạo, workflow không bao giờ ghi đè
├── docker-compose.prod.yml    # workflow scp lên mỗi lần deploy
└── runtime/                   # job + media, mount vào container
```

SSH vào VM và chạy:

```bash
sudo mkdir -p /opt/autocc/runtime && sudo chown -R $USER:$USER /opt/autocc
```

Nếu VM chưa có Docker:

```bash
curl -fsSL https://get.docker.com | sudo sh && sudo usermod -aG docker $USER && newgrp docker
```

Tạo `/opt/autocc/.env` theo mẫu [.env.example](.env.example), điền các key thật
(`DEEPGRAM_API_KEY`, `LLM_API_KEY`…). Thêm hai dòng riêng cho production:

```bash
STATIC_CACHE_SECONDS=3600
# Đổi cổng public nếu muốn khác 8000
# AUTOCC_PORT=8000
```

> `STATIC_CACHE_SECONDS=0` là giá trị cho môi trường dev. Trên VM để `0` nghĩa là
> mọi file frontend đều `no-cache`, mỗi lần load trang là một loạt request thừa.
> Nhưng asset không có content hash trong tên, nên sau mỗi deploy phải hard
> refresh — cân nhắc trước khi đặt số lớn.

## 2. Xác thực SSH (Mật khẩu hoặc SSH Key)

Mặc định workflow [ci.yml](.github/workflows/ci.yml) đang dùng **mật khẩu** (`VM_SSH_PASSWORD`). Bạn chỉ cần khai báo mật khẩu của user VM vào GitHub Secrets.

Trước đó phải chắc VM cho phép đăng nhập bằng mật khẩu — VM Azure tạo bằng SSH key mặc định tắt tuỳ chọn này, và workflow sẽ fail ngay ở bước SSH với lỗi `Permission denied (publickey)`:

```bash
sudo sshd -T | grep -i passwordauthentication
```

Nếu kết quả là `no`, bật lên (Azure hay ghi đè cấu hình trong `sshd_config.d/`, nên phải sửa cả hai chỗ):

```bash
sudo sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config
sudo grep -rl PasswordAuthentication /etc/ssh/sshd_config.d/ | xargs -r sudo sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication yes/'
sudo systemctl restart ssh
```

> Mở password auth trên port 22 công khai nghĩa là VM sẽ bị bot dò mật khẩu liên tục. Tối thiểu nên đặt mật khẩu dài, ngẫu nhiên và cài `fail2ban` (`sudo apt install -y fail2ban`). Không whitelist IP runner GitHub được vì dải IP của họ rất rộng và thay đổi thường xuyên.

*(Tuỳ chọn nếu muốn dùng SSH Key thay vì Mật khẩu)*:
1. Tạo key: `ssh-keygen -t ed25519 -C "github-actions-autocc" -f ~/.ssh/autocc_deploy -N ""`
2. Copy lên VM: `ssh-copy-id -i ~/.ssh/autocc_deploy.pub <user>@<ip-vm>`
3. Đổi dòng `password: ${{ secrets.VM_SSH_PASSWORD }}` trong `ci.yml` thành `key: ${{ secrets.VM_SSH_KEY }}`.

## 3. Khai báo secrets & variables trên GitHub

`Settings → Secrets and variables → Actions`:

| Loại | Tên | Giá trị |
|---|---|---|
| Secret | `VM_HOST` | IP public hoặc DNS của VM |
| Secret | `VM_USER` | user SSH (thường là `azureuser`) |
| Secret | `VM_SSH_PASSWORD` | Mật khẩu đăng nhập của Azure VM |
| Secret | `VM_SSH_PORT` | *(tuỳ chọn)* cổng SSH nếu khác `22` |
| Variable | `DEPLOY_PATH` | *(tuỳ chọn)* đường dẫn khác `/opt/autocc` |

`GITHUB_TOKEN` để pull image từ GHCR do Actions tự cấp, hết hạn khi job kết
thúc — không cần tạo PAT và VM không giữ credential nào lâu dài.

## 4. Mở cổng trên Azure NSG

> **App không có lớp xác thực nào.** Bất kỳ ai gõ đúng `http://<ip-vm>:8000` đều
> upload được video, đọc được job của bạn và tiêu API key Deepgram/Mistral trong
> `.env`. Đừng mở 8000 cho `0.0.0.0/0` — giới hạn theo IP của bạn:

```bash
az network nsg rule create \
  --resource-group <resource-group> --nsg-name <ten-nsg> \
  --name autocc-http --priority 1010 \
  --source-address-prefixes <ip-cua-ban>/32 \
  --destination-port-ranges 8000 --access Allow --protocol Tcp
```

Nếu chấp nhận rủi ro và vẫn muốn mở công khai (ví dụ đang demo), lệnh ngắn là
`az vm open-port --resource-group <rg> --name <ten-vm> --port 8000 --priority 1010`
— nhưng nên bật basic auth qua nginx trước, xem ghi chú bên dưới.

Cổng SSH (22) cũng phải mở cho GitHub Actions. Nếu NSG của bạn đang khoá 22 theo
IP, hoặc phải whitelist dải IP runner của GitHub, hoặc chuyển sang self-hosted
runner đặt ngay trên VM.

> Port 8000 trần nghĩa là HTTP thuần, không TLS, và giới hạn upload chỉ còn dựa
> vào `MAX_UPLOAD_MB` trong app. Khi nào cần HTTPS thì dựng nginx theo
> [nginx.conf.example](nginx.conf.example), đổi `ports` trong compose thành
> `127.0.0.1:8000:8000` rồi đóng 8000 trên NSG.

## 5. Nếu VM đang chạy app theo cách cũ

Container mới cũng tên `autocc` và cũng chiếm port 8000, nên phải hạ stack cũ
trước lần deploy đầu, đồng thời giữ lại dữ liệu `runtime/`:

```bash
cd <thu-muc-cu> && docker compose down
sudo cp -a runtime/. /opt/autocc/runtime/
```

Nếu app đang chạy bằng systemd/uvicorn trực tiếp thay vì Docker thì
`sudo systemctl disable --now <ten-service>` trước khi deploy.

## 6. Chạy thử

```bash
git push origin main
```

Xem tiến trình ở tab **Actions**. Job `deploy` kết thúc bằng một smoke test gọi
`/api/health` trên VM; nếu container không lên, log 100 dòng cuối được in ra
ngay trong output của job.

## 7. Rollback

Mỗi image được tag bằng commit SHA nên quay lại bản cũ không cần build lại. Lấy
SHA của commit tốt gần nhất rồi chạy trên VM:

```bash
cd /opt/autocc && AUTOCC_IMAGE=ghcr.io/tunakite03/autoccstudio:<sha-cu> docker compose -f docker-compose.prod.yml up -d
```

Xem các tag đang có tại `https://github.com/Tunakite03/AutoCCStudio/pkgs/container/autoccstudio`.
