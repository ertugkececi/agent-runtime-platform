"""Sign in to Codex with ChatGPT without a separate CLI installation."""

from __future__ import annotations

import sys


def main() -> int:
    from openai_codex import Codex

    try:
        with Codex() as codex:
            account = codex.account().account
            if account is not None and account.root.type == "chatgpt":
                print("ChatGPT oturumu zaten açık.")
                return 0

            login = codex.login_chatgpt_device_code()
            print(f"Tarayıcıda aç: {login.verification_url}", flush=True)
            print(f"Giriş kodu: {login.user_code}", flush=True)
            try:
                completed = login.wait()
            except KeyboardInterrupt:
                login.cancel()
                print("\nGiriş iptal edildi.", file=sys.stderr)
                return 130
            if not completed.success:
                print("ChatGPT girişi tamamlanamadı.", file=sys.stderr)
                return 1
            print("ChatGPT girişi tamamlandı.")
            return 0
    except Exception as exc:
        print(
            f"ChatGPT girişi başlatılamadı ({type(exc).__name__}). "
            "Hesabında Codex cihaz koduyla girişi etkinleştirip yeniden dene.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
