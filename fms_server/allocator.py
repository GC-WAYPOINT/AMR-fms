"""FMS 게시판 배정 루프. 로봇 변경 보고에 의존하지 않고 신규 게시물도 주기적으로 확인한다."""
import logging
import threading

log = logging.getLogger('fms.allocator')


class JobAllocator:
    def __init__(self, coordinator, ticks=()):
        self.coordinator = coordinator
        # allocate_pending 뒤에 순서대로 부르는 주기 작업 (이름, 함수). 새 스레드를 만들지 않고 여기에 얹는다.
        self.ticks = list(ticks)
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name='job-allocator', daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.wait(1.0):
            try:
                self.coordinator.allocate_pending()
            except Exception:
                log.exception('게시판 배정 중 오류')
            for name, tick in list(self.ticks):
                try:
                    tick()
                except Exception:
                    log.exception('주기 작업 %s 오류', name)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join()
