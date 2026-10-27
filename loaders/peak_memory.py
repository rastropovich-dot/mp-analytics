"""Пик памяти процесса для лога шага (59-я): Render убивает ночь на 2 ГиБ (27.10), а сам пик не пишет — пишем мы."""
import resource
import sys


def peak_bytes():
    """Максимальный RSS процесса в байтах: ru_maxrss — килобайты на Linux (Render), байты на macOS."""
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss if sys.platform == "darwin" else rss * 1024


def line(step):
    return f"пик памяти шага «{step}»: {peak_bytes() / 1024 / 1024:,.0f} МБ (max RSS; лимит Render 2 048 МБ)"
