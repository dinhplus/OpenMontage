#!/usr/bin/env python3
"""Kiểm tra một khoá Google TTS dùng được hay không — TRƯỚC khi giao cho pipeline.

Vì sao có file này: đổi sang tài khoản Google mới là chuỗi sáu bước (bật API, tạo
service account, cấp quyền, tải khoá, nối billing, trỏ .env) và bước nào hỏng cũng
cho ra cùng một triệu chứng — narrate.py chết giữa chừng sau khi đã sinh vài đoạn,
tức đã tốn tiền. Script này đi hết chuỗi đó bằng MỘT câu mười mấy ký tự.

    .venv/bin/python tools/audio/check_tts_auth.py

Đọc GOOGLE_APPLICATION_CREDENTIALS trong .env (hoặc biến môi trường sẵn có).
"""
from __future__ import annotations
import base64, json, os, sys, pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

def nap_env() -> None:
    f = REPO / ".env"
    if not f.exists(): return
    for dong in f.read_text(encoding="utf-8").splitlines():
        dong = dong.strip()
        if not dong or dong.startswith("#") or "=" not in dong: continue
        k, v = dong.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

CAU = "Xin chào."                      # ngắn nhất có thể — phép thử không được tốn tiền

def main() -> int:
    nap_env()
    p = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    print(f"1. GOOGLE_APPLICATION_CREDENTIALS = {p or '(chưa đặt)'}")
    if not p or not os.path.exists(p):
        print("   HỎNG: không thấy file khoá. Sửa dòng GOOGLE_APPLICATION_CREDENTIALS trong .env")
        return 1
    try:
        d = json.loads(pathlib.Path(p).read_text(encoding="utf-8"))
    except Exception as e:
        print(f"   HỎNG: file khoá không đọc được ({e})"); return 1
    if d.get("type") != "service_account":
        print(f"   HỎNG: type = {d.get('type')!r}, cần 'service_account'.")
        print("   Chirp3-HD KHÔNG nhận API key — phải là khoá service account.")
        return 1
    print(f"   OK · project {d.get('project_id')} · {d.get('client_email')}")

    print("2. Xin access token…")
    try:
        from tools.google_credentials import get_access_token
        # TRẢ VỀ TUPLE (token, project_id) — không phải chuỗi. Bản đầu của script này
        # gán thẳng vào `tok` rồi in ra "token dài 2 ký tự" mà vẫn đi tiếp, tức phép
        # kiểm tự báo đạt trên một thứ nó chưa hề kiểm.
        tok, proj_key = get_access_token()
    except Exception as e:
        print(f"   HỎNG: {type(e).__name__}: {e}")
        print("   Thường là thiếu gói google-auth, hoặc khoá đã bị thu hồi.")
        return 1
    if not isinstance(tok, str) or len(tok) < 50:
        print(f"   HỎNG: token không hợp lệ ({tok!r})"); return 1
    print(f"   OK · token dài {len(tok)} ký tự · project theo khoá: {proj_key}")

    print(f"3. Gọi thật Chirp3-HD ({len(CAU)} ký tự)…")
    # Dùng `requests` y như tools/audio/google_tts.py. urllib của .venv này không
    # tìm được chứng chỉ gốc (CERTIFICATE_VERIFY_FAILED) — phép kiểm mà hỏng khác
    # cách công cụ thật chạy thì nó kiểm nhầm thứ.
    import requests
    body = json.dumps({
        "input": {"text": CAU},
        "voice": {"languageCode": "vi-VN", "name": "vi-VN-Chirp3-HD-Charon"},
        "audioConfig": {"audioEncoding": "LINEAR16"},
    }).encode()
    try:
        r = requests.post(
            "https://texttospeech.googleapis.com/v1/text:synthesize",
            data=body,
            headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
            timeout=60,
        )
    except Exception as e:
        print(f"   HỎNG: {type(e).__name__}: {e}"); return 1
    if r.status_code != 200:
        print(f"   HỎNG: HTTP {r.status_code}\n   {r.text[:600]}")
        if r.status_code == 403:
            print("   -> Thường là: chưa bật Text-to-Speech API, hoặc chưa nối billing,")
            print("      hoặc service account thiếu quyền dùng dịch vụ của project.")
        return 1
    ra = r.json()

    n = len(base64.b64decode(ra.get("audioContent", "")))
    print(f"   OK · nhận về {n:,} byte audio")
    print(f"\nDÙNG ĐƯỢC. Đã tiêu đúng {len(CAU)} ký tự của hạn mức tháng này.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
