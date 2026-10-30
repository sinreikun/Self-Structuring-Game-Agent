import re


def error_message(error: Exception) -> str:
    from urllib.error import HTTPError, URLError
    if isinstance(error, PermissionError):
        return "保存先または実行に必要な権限がありません。保存先と対象ゲームの権限を確認してください。"
    if isinstance(error, HTTPError):
        return "配布情報または推論先が要求を受け付けませんでした。接続設定と配布元の状態を確認してください。"
    if isinstance(error, (URLError, ConnectionError, TimeoutError)):
        return "接続を確認できませんでした。既存の経験は保持しています。通信設定またはローカル推論の状態を確認してください。"
    if isinstance(error, (ValueError, OSError)) and re.search('[ぁ-んァ-ン一-龥]', str(error)):
        return prose(str(error))
    return "内部処理に問題が発生しました。技術情報を開発者詳細へ記録しました。"


def prose(text: str) -> str:
    """Keep machine details out of normal views; preserve originals in diagnostics."""
    if re.search(r"```|Traceback|\{[\s\S]*\}|(?:[A-Za-z]:\\|/(?:Users|home|tmp)/)|\b(?:def|class|import)\s|\(module|(?:\w+_){2,}", text):
        return "研究内容の詳細を記録しました。技術情報は開発者詳細で確認できます。"
    return text[:4000]


def system_info() -> dict:
    import os
    import platform
    import shutil
    import ctypes as c
    ram = 0
    if os.name == "nt":
        class Status(c.Structure):
            _fields_ = [("length", c.c_uint32), ("load", c.c_uint32)] + [(name, c.c_uint64) for name in (
                "total", "available", "page_total", "page_available", "virtual_total", "virtual_available", "extended")]
        status = Status(length=c.sizeof(Status))
        if c.windll.kernel32.GlobalMemoryStatusEx(c.byref(status)):
            ram = status.available / 1024**3
    else:
        try:
            ram = os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 1024**3
        except (ValueError, OSError):
            pass
    return {"cpu": platform.processor() or platform.machine(), "ram": ram,
            "free": shutil.disk_usage(os.path.expanduser("~")).free / 1024**3}


def event_message(kind: str, data: dict | None = None) -> str:
    data=data or {}
    messages={
        'llm_start':'研究用LLMへ分析を依頼しました。',
        'llm_timeout':'研究用LLMの応答が時間内に完了しませんでした。後で再試行します。',
        'llm_error':'研究用LLMの応答を確認できませんでした。後で再試行します。',
        'replay':'新しい改善案を過去経験で検証しています。',
        'rejected':'改善候補を試しましたが、現在の方法を上回らなかったため採用しませんでした。',
        'adopted':'新しい能力を採用しました。画面変化の予測方法を改善しました。',
        'comparison':'同じ条件で、新しい方法と現在の方法を比較しています。',
        'recovery':'ゲームとの接続を復旧しています。',
        'error':'異常の前後の観測を保護しました。診断情報へ記録しています。',
        'gc':'保存領域を自動整理しています。研究の根拠は保持します。',
        'change':'画面変化を確認しました。',
        'unchanged':'入力前後で大きな画面変化はありませんでした。',
    }
    if kind=='action':
        from .windows import KEY_CODES
        key=data.get('key','')
        return f'{key}を入力しました。' if key in KEY_CODES else '許可された入力を実行しました。'
    return messages.get(kind,'研究の進行を記録しました。')


def gpu_info() -> list[str]:
    import os
    if os.name!='nt':return ['未取得']
    import ctypes as c
    from ctypes import wintypes as w
    class Device(c.Structure):
        _fields_=[('cb',w.DWORD),('name',w.WCHAR*32),('description',w.WCHAR*128),('flags',w.DWORD),('id',w.WCHAR*128),('key',w.WCHAR*128)]
    result=[];i=0
    while i<32:
        device=Device(cb=c.sizeof(Device))
        if not c.windll.user32.EnumDisplayDevicesW(None,i,c.byref(device),0):break
        if device.description:result.append(device.description)
        i+=1
    return result or ['未取得']
