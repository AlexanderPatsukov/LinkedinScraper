"""Фоновое выполнение парсинга.

Запуск живёт в отдельном потоке внутри процесса Django, а всё состояние
(статус, счётчики, флаг остановки, очередь задач) хранится в БД. Благодаря
этому остановить запуск можно из любого воркера gunicorn и из любой вкладки
админки: поток перечитывает флаг `stop_requested` перед каждым HTTP-запросом.

Два уровня работы:

1. ОЧЕРЕДЬ ЗАДАЧ. LinkedIn отдаёт максимум 1000 результатов на одну комбинацию
   параметров (start=1000 отвечает HTTP 400). Задача, упёршаяся в этот потолок,
   делится на подзадачи - по локациям, затем по датам, затем по уточняющим
   словам - и каждая получает свою тысячу. Дерево задач и есть «место, на
   котором мы остановились».

2. ПОСТРАНИЧНАЯ ОБРАБОТКА внутри задачи: собрали десятку карточек - тут же
   догрузили их описания и профили компаний - и только потом идём за следующей
   страницей. Остановка в любой момент оставляет за собой полностью
   обработанные страницы.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from collections import Counter

from django.conf import settings
from django.db import IntegrityError, connections, transaction
from django.utils import timezone

from .models import (
    Company,
    CrawlTask,
    DatePosted,
    ParseRun,
    RunStatus,
    SearchQuery,
    SplitAxis,
    TaskStatus,
    Vacancy,
    VacancyStatus,
)
from .parser import (
    Blocked,
    HARD_PAGE_CAP,
    JobCard,
    LinkedInGuestClient,
    RESULTS_PER_PAGE,
    ScraperError,
)

logger = logging.getLogger(__name__)

# Сколько блокировок подряд терпим, прежде чем отправить фазу на паузу.
# Долбиться дальше бессмысленно и только продлевает бан, но и выключать фазу
# насовсем нельзя: 999 транзиентный, через несколько минут те же URL снова
# отдают 200.
CONSECUTIVE_BLOCK_LIMIT = 3

# Окна дат для деления по оси «дата публикации». Окна вложенные, а не
# непересекающиеся (f_TPR - это «за последние N секунд» от текущего момента),
# так что это не настоящее разбиение, а способ добраться до свежего слоя,
# который в общей выдаче остался за тысячным результатом. Дубли съедает дедуп.
DATE_WINDOW_LADDER = [DatePosted.MONTH, DatePosted.WEEK, DatePosted.DAY]


class StopRequested(Exception):
    """Пользователь нажал «Остановить»."""


class BudgetExhausted(Exception):
    """Исчерпан бюджет запросов на запуск."""


class _Phase:
    """Состояние одной фазы (описания или компании) в пределах запуска.

    При серии блокировок фаза уходит на ПАУЗУ, а не выключается насовсем.
    999 от LinkedIn транзиентный: те же самые URL через несколько минут снова
    отдают 200. Глухое выключение означало бы, что три неудачи в начале
    многочасового прогона лишают компаний весь прогон целиком.
    """

    def __init__(self, run: ParseRun, label: str, cooldown: float):
        self.run = run
        self.label = label
        self.base_cooldown = cooldown
        self.blocks_in_row = 0
        self.pauses = 0
        self.paused_until: float | None = None
        self.stopped = False  # исчерпан лимит - это уже навсегда

    @property
    def off(self) -> bool:
        if self.stopped:
            return True
        if self.paused_until is None:
            return False
        if time.monotonic() >= self.paused_until:
            self.paused_until = None
            self.blocks_in_row = 0
            self.run.append_log(f"Пауза по {self.label} вышла - пробую снова.", save=False)
            return False
        return True

    def note(self, result: str) -> None:
        if result == "blocked":
            self.blocks_in_row += 1
            if self.blocks_in_row >= CONSECUTIVE_BLOCK_LIMIT:
                self._pause()
        else:
            self.blocks_in_row = 0

    def stop(self, reason: str) -> None:
        if not self.stopped:
            self.stopped = True
            self.run.append_log(reason, save=False)

    def _pause(self) -> None:
        self.pauses += 1
        # каждая следующая пауза вдвое длиннее, но не больше часа
        cooldown = min(self.base_cooldown * (2 ** (self.pauses - 1)), 3600)
        self.paused_until = time.monotonic() + cooldown
        self.blocks_in_row = 0
        self.run.append_log(
            f"{CONSECUTIVE_BLOCK_LIMIT} блокировки подряд - {self.label} на паузе "
            f"{int(cooldown // 60)} мин {int(cooldown % 60)} с, потом попробую снова.",
            save=False,
        )


class _Budget:
    """Бюджет запуска: паузы по блокировкам и лимит профилей компаний."""

    def __init__(self, run: ParseRun, max_company_profiles: int = 0, cooldown: float = 600.0):
        self.run = run
        self.max_company_profiles = max_company_profiles
        self.companies_fetched = 0
        self._companies = _Phase(run, "загрузку профилей компаний", cooldown)
        self._details = _Phase(run, "загрузку описаний", cooldown)

    @property
    def companies_off(self) -> bool:
        return self._companies.off

    @property
    def details_off(self) -> bool:
        return self._details.off

    def note_company(self, result: str) -> None:
        if result == "ok":
            self.companies_fetched += 1
        self._companies.note(result)

        if self.max_company_profiles and self.companies_fetched >= self.max_company_profiles:
            self._companies.stop(
                f"Достигнут лимит профилей компаний ({self.max_company_profiles}) - "
                "дальше только названия и ссылки."
            )

    def note_detail(self, result: str) -> None:
        self._details.note(result)


def start_run(search_query: SearchQuery, *, backfill_only: bool = False) -> tuple[ParseRun | None, str]:
    """Создаёт запуск и поднимает поток. Возвращает (run, сообщение об ошибке)."""
    live = ParseRun.objects.filter(
        search_query=search_query,
        status__in=[RunStatus.PENDING, RunStatus.RUNNING, RunStatus.STOPPING],
    ).first()
    if live:
        return None, f"Запрос «{search_query.title}» уже выполняется (запуск #{live.pk})."

    run = ParseRun.objects.create(
        search_query=search_query, status=RunStatus.PENDING, backfill_only=backfill_only
    )
    limit = "без лимита" if search_query.max_requests == 0 else f"{search_query.max_requests} зап."
    run.append_log(
        f"Запуск создан. Запрос: {search_query.keywords!r}, локация: {search_query.country}, лимит: {limit}."
    )
    spawn(run.pk)
    return run, ""


def spawn(run_id: int) -> None:
    thread = threading.Thread(target=_thread_main, args=(run_id,), name=f"parse-run-{run_id}", daemon=True)
    thread.start()


def request_stop(run: ParseRun) -> bool:
    if not run.is_live:
        return False
    ParseRun.objects.filter(pk=run.pk).update(stop_requested=True, status=RunStatus.STOPPING)
    return True


def _thread_main(run_id: int) -> None:
    try:
        execute_run(run_id)
    except Exception:  # noqa: BLE001 - поток не должен падать молча
        logger.exception("Запуск #%s упал с необработанной ошибкой", run_id)
        ParseRun.objects.filter(pk=run_id).update(
            status=RunStatus.FAILED,
            error_message=traceback.format_exc()[-4000:],
            finished_at=timezone.now(),
        )
    finally:
        connections.close_all()


def execute_run(run_id: int) -> None:
    run = ParseRun.objects.select_related("search_query").get(pk=run_id)
    query = run.search_query

    run.status = RunStatus.RUNNING
    run.started_at = timezone.now()
    run.save(update_fields=["status", "started_at"])

    client = LinkedInGuestClient(
        user_agent=settings.SCRAPER_USER_AGENT,
        timeout=query.request_timeout or settings.SCRAPER_DEFAULT_TIMEOUT,
        delay=query.delay_seconds if query.delay_seconds is not None else settings.SCRAPER_DEFAULT_DELAY,
        max_retries=settings.SCRAPER_MAX_RETRIES,
    )
    budget = _Budget(
        run,
        max_company_profiles=query.max_company_profiles,
        cooldown=settings.SCRAPER_BLOCK_COOLDOWN,
    )

    final_status = RunStatus.DONE
    try:
        with client:
            if run.backfill_only:
                _backfill(run, query, client, budget)
            else:
                _crawl(run, query, client, budget)
    except StopRequested:
        final_status = RunStatus.STOPPED
        run.append_log("Остановлено пользователем.", save=False)
    except BudgetExhausted:
        run.append_log(
            f"Исчерпан бюджет запросов ({query.max_requests}). Незавершённые задачи остались "
            "в очереди - запустите запрос снова, он продолжит с них.",
            save=False,
        )
    except Blocked as exc:
        final_status = RunStatus.FAILED
        run.error_message = str(exc)
        run.append_log(f"Доступ закрыт: {exc}", save=False)
    except ScraperError as exc:
        final_status = RunStatus.FAILED
        run.error_message = str(exc)
        run.append_log(f"Ошибка парсера: {exc}", save=False)
    except Exception as exc:  # noqa: BLE001
        final_status = RunStatus.FAILED
        run.error_message = traceback.format_exc()[-4000:]
        run.append_log(f"Непредвиденная ошибка: {exc}", save=False)
        logger.exception("Запуск #%s", run_id)

    # Задачу, оставшуюся в RUNNING после обрыва, возвращаем в очередь
    CrawlTask.objects.filter(search_query=query, status=TaskStatus.RUNNING).update(status=TaskStatus.PENDING)

    run.status = final_status
    run.finished_at = timezone.now()
    pending = CrawlTask.objects.filter(search_query=query, status=TaskStatus.PENDING).count()
    run.append_log(
        "Итого: страниц {pages}, карточек {seen}, новых {created}, обновлено {updated}, "
        "описаний {details}, профилей компаний {companies}, ошибок {errors}. "
        "Задач в очереди осталось: {pending}.".format(
            pages=run.requests_made,
            seen=run.jobs_seen,
            created=run.jobs_created,
            updated=run.jobs_updated,
            details=run.detail_requests,
            companies=run.company_requests,
            errors=run.errors_count,
            pending=pending,
        ),
        save=False,
    )
    run.save()


def _check_stop(run: ParseRun) -> None:
    fresh = ParseRun.objects.filter(pk=run.pk).values("stop_requested").first()
    if fresh and fresh["stop_requested"]:
        raise StopRequested


def _check_budget(run: ParseRun, query: SearchQuery) -> None:
    if query.max_requests and run.requests_made >= query.max_requests:
        raise BudgetExhausted


def _save_counters(run: ParseRun) -> None:
    run.save(
        update_fields=[
            "requests_made",
            "detail_requests",
            "company_requests",
            "jobs_seen",
            "jobs_created",
            "jobs_updated",
            "companies_created",
            "errors_count",
            "log",
        ]
    )


# ------------------------------------------------------------ очередь задач

def ensure_root_task(query: SearchQuery) -> CrawlTask:
    task, _created = CrawlTask.objects.get_or_create(
        search_query=query,
        keywords=query.keywords,
        location=query.root_location,
        geo_id=query.geo_id,
        date_posted=query.date_posted,
        defaults={"depth": 0, "split_axis": SplitAxis.ROOT},
    )
    return task


def _next_task(query: SearchQuery) -> CrawlTask | None:
    """Сначала неисследованные задачи, потом начатые.

    Порядок именно такой, иначе корень с его тысячей результатов съедает
    бюджет каждого запуска и подзадачи не получают очередь никогда.
    """
    return (
        CrawlTask.objects.filter(search_query=query, status=TaskStatus.PENDING)
        .select_related("parent")
        .order_by("pages_done", "depth", "pk")
        .first()
    )


def _crawl(run: ParseRun, query: SearchQuery, client: LinkedInGuestClient, budget: _Budget) -> None:
    root = ensure_root_task(query)

    # Новый цикл начинаем, только когда разобрана вся очередь: иначе повторный
    # запуск бесконечно перечитывал бы корень вместо незакрытых подзадач.
    if not CrawlTask.objects.filter(search_query=query, status=TaskStatus.PENDING).exists():
        CrawlTask.objects.filter(pk=root.pk).update(status=TaskStatus.PENDING, pages_done=0)
        run.append_log("Очередь пуста - начинаю новый цикл с корневой задачи.", save=False)
        _save_counters(run)

    while True:
        _check_stop(run)
        _check_budget(run, query)

        task = _next_task(query)
        if task is None:
            run.append_log("Очередь задач пуста - собирать больше нечего.", save=False)
            _save_counters(run)
            return

        _process_task(run, query, client, budget, task)


def _process_task(
    run: ParseRun,
    query: SearchQuery,
    client: LinkedInGuestClient,
    budget: _Budget,
    task: CrawlTask,
) -> None:
    task.status = TaskStatus.RUNNING
    task.last_run = run
    task.save(update_fields=["status", "last_run", "updated_at"])

    resumed = f", продолжаю со страницы {task.pages_done + 1}" if task.pages_done else ""
    run.append_log(f"Задача #{task.pk} (глубина {task.depth}): {task.label}{resumed}", save=False)
    _save_counters(run)

    saturated = False
    try:
        saturated = _crawl_task_pages(run, query, client, budget, task)
    except (StopRequested, BudgetExhausted):
        task.status = TaskStatus.PENDING
        task.save(update_fields=["status", "updated_at"])
        raise
    except Blocked:
        task.status = TaskStatus.PENDING
        task.save(update_fields=["status", "updated_at"])
        raise
    except ScraperError as exc:
        task.status = TaskStatus.FAILED
        task.note = str(exc)[:2000]
        task.finished_at = timezone.now()
        task.save(update_fields=["status", "note", "finished_at", "updated_at"])
        run.errors_count += 1
        run.append_log(f"Задача #{task.pk}: {exc}", save=False)
        _save_counters(run)
        return

    task.status = TaskStatus.SATURATED if saturated else TaskStatus.DONE
    task.finished_at = timezone.now()
    task.save(update_fields=["status", "finished_at", "updated_at"])

    if saturated:
        children = _split_task(run, query, task)
        if children:
            run.append_log(
                f"Задача #{task.pk} упёрлась в потолок (1000) - разделена на {children} подзадач.",
                save=False,
            )
        else:
            run.append_log(
                f"Задача #{task.pk} упёрлась в потолок, но делить дальше нечем "
                "(исчерпаны оси или лимиты дробления).",
                save=False,
            )
        _save_counters(run)


def _crawl_task_pages(
    run: ParseRun,
    query: SearchQuery,
    client: LinkedInGuestClient,
    budget: _Budget,
    task: CrawlTask,
) -> bool:
    """Листает страницы одной задачи. True - упёрлись в потолок LinkedIn.

    Задача продолжается с того места, где её прервали: pages_done - это не
    счётчик прохода, а позиция в выдаче. Иначе задача с бюджетом в 10 страниц
    вечно перечитывала бы первые десять и никогда не дошла бы до глубины.
    """
    last_page_full = False

    for page in range(task.pages_done, HARD_PAGE_CAP):
        _check_stop(run)
        _check_budget(run, query)
        start = page * RESULTS_PER_PAGE

        cards = _fetch_page(run, client, task, start)
        if cards is None:
            continue  # страница не удалась, но задачу не роняем
        if not cards:
            run.append_log(f"  start={start}: пусто - результаты закончились.", save=False)
            _save_counters(run)
            return False

        task.pages_done = page + 1
        task.found_count += len(cards)
        run.jobs_seen += len(cards)
        last_page_full = len(cards) >= RESULTS_PER_PAGE

        page_vacancy_ids: list[int] = []
        for card in cards:
            try:
                vacancy, created, needs_detail = _upsert_vacancy(run, query, card, task)
            except Exception as exc:  # noqa: BLE001 - кривая карточка не ломает страницу
                run.errors_count += 1
                run.append_log(f"  job_id={card.job_id}: {exc}", save=False)
                logger.exception("Карточка %s", card.job_id)
                continue
            if created:
                run.jobs_created += 1
                task.new_count += 1
            else:
                run.jobs_updated += 1
            if needs_detail:
                page_vacancy_ids.append(vacancy.pk)

        task.save(update_fields=["pages_done", "found_count", "new_count", "updated_at"])
        run.append_log(f"  start={start}: карточек {len(cards)}, новых в задаче {task.new_count}.", save=False)
        _save_counters(run)

        # Обрабатываем ЭТУ ЖЕ десятку, пока не ушли дальше по списку.
        # Описания идут первыми: это главные данные, и job-эндпоинт почти не
        # лимитируется, тогда как страницы компаний придерживаются регулярно.
        if query.fetch_descriptions and not budget.details_off:
            _process_details(run, client, budget, page_vacancy_ids)
        if query.fetch_companies and not budget.companies_off:
            _process_companies(run, client, budget, vacancy_ids=page_vacancy_ids)

    # Прошли все 100 страниц, и последняя была полной - значит выдача упёрлась
    # в потолок, а не закончилась.
    return last_page_full


def _fetch_page(
    run: ParseRun, client: LinkedInGuestClient, task: CrawlTask, start: int
) -> list[JobCard] | None:
    """Страница списка. [] - выдача кончилась, None - запрос не удался.

    Пустая страница перепроверяется один раз: замерами видно, что LinkedIn
    изредка отдаёт пустое тело (26 байт) в середине выдачи, а при повторе на
    том же смещении возвращает все 10 карточек.
    """
    for attempt in (1, 2):
        try:
            cards = client.search_page(task.build_params(start=start))
        except ScraperError as exc:
            run.requests_made += 1
            run.errors_count += 1
            run.append_log(f"  start={start}: {exc}", save=False)
            _save_counters(run)
            if isinstance(exc, Blocked):
                raise
            return None
        run.requests_made += 1

        if cards:
            return cards
        if attempt == 1:
            run.append_log(f"  start={start}: пусто, перепроверяю.", save=False)
            _save_counters(run)

    return []


# --------------------------------------------------------------- дробление

def _split_task(run: ParseRun, query: SearchQuery, task: CrawlTask) -> int:
    """Создаёт подзадачи для насыщенной задачи. Возвращает их количество."""
    if not query.split_enabled or task.depth >= query.max_depth:
        return 0
    if query.max_tasks and CrawlTask.objects.filter(search_query=query).count() >= query.max_tasks:
        run.append_log(f"Достигнут лимит задач на запрос ({query.max_tasks}) - дробить не буду.", save=False)
        return 0

    used = task.axes_used()
    for axis, builder in (
        (SplitAxis.LOCATION, _children_by_location),
        (SplitAxis.DATE, _children_by_date),
        (SplitAxis.KEYWORDS, _children_by_keywords),
    ):
        if axis in used:
            continue
        facets = builder(query, task)
        if not facets:
            continue
        created = _create_children(query, task, axis, facets)
        if created:
            return created
    return 0


def _create_children(query: SearchQuery, task: CrawlTask, axis: str, facets: list[dict]) -> int:
    created = 0
    room = None
    if query.max_tasks:
        room = query.max_tasks - CrawlTask.objects.filter(search_query=query).count()
        if room <= 0:
            return 0

    for facet in facets:
        if room is not None and created >= room:
            break
        payload = {
            "keywords": task.keywords,
            "location": task.location,
            "geo_id": task.geo_id,
            "date_posted": task.date_posted,
            **facet,
        }
        try:
            with transaction.atomic():
                child, was_created = CrawlTask.objects.get_or_create(
                    search_query=query,
                    keywords=payload["keywords"],
                    location=payload["location"],
                    geo_id=payload["geo_id"],
                    date_posted=payload["date_posted"],
                    defaults={
                        "parent": task,
                        "depth": task.depth + 1,
                        "split_axis": axis,
                    },
                )
        except IntegrityError:
            continue
        if was_created:
            created += 1
        elif child.status == TaskStatus.DONE:
            # новый цикл сбора: подзадача уже существует, но пройдена - возвращаем
            # её в очередь, иначе повторный запуск обходил бы только корень
            CrawlTask.objects.filter(pk=child.pk).update(status=TaskStatus.PENDING, pages_done=0)
            created += 1
    return created


def _children_by_location(query: SearchQuery, task: CrawlTask) -> list[dict]:
    """Локации берём из вакансий, которые нашла ИМЕННО ЭТА задача.

    Хардкодить geoId не нужно: LinkedIn резолвит строку локации, а формат,
    в котором он сам её отдаёт, гарантированно резолвится обратно. Брать
    строку как есть важно: голое имя города проходит не всегда
    (location=Lviv отдаёт 0 результатов, а «Lviv, Lviv, Ukraine» - полную
    страницу).

    Отбор по вакансиям задачи, а не по строковому вхождению в родителя:
    LinkedIn отдаёт локации в разных форматах, и для США это «San Francisco,
    CA» - без названия страны, так что проверка «заканчивается на родителя»
    отсекала бы вообще всё.
    """
    if task.geo_id:
        return []

    parent = (task.location or "").strip()
    raw = (
        Vacancy.objects.filter(last_task=task)
        .exclude(location="")
        .values_list("location", flat=True)
    )
    counter = Counter(value.strip() for value in raw if value and value.strip())
    counter.pop(parent, None)

    ancestors = set()
    node = task
    while node is not None:
        if node.location:
            ancestors.add(node.location.strip())
        node = node.parent

    candidates: list[dict] = []
    for value, _count in counter.most_common():
        if value in ancestors:
            continue
        candidates.append({"location": value})
        if len(candidates) >= query.split_locations_limit:
            break
    return candidates


def _children_by_date(query: SearchQuery, task: CrawlTask) -> list[dict]:
    current = task.date_posted
    ladder = [window for window in DATE_WINDOW_LADDER if window != current]
    if current:
        # сужаем только в сторону более свежих окон
        try:
            position = DATE_WINDOW_LADDER.index(current)
        except ValueError:
            position = -1
        ladder = DATE_WINDOW_LADDER[position + 1 :]
    return [{"date_posted": window} for window in ladder]


def _children_by_keywords(query: SearchQuery, task: CrawlTask) -> list[dict]:
    words = query.split_keyword_list()
    if not words:
        return []
    base = task.keywords.strip()
    children = []
    for word in words:
        if word.lower() in base.lower():
            continue
        children.append({"keywords": f"{base} {word}"[:255]})
    return children


# ----------------------------------------------------------------- вакансии

@transaction.atomic
def _upsert_vacancy(
    run: ParseRun, query: SearchQuery, card: JobCard, task: CrawlTask | None = None
) -> tuple[Vacancy, bool, bool]:
    """Дедупликация по job_id: вторая встреча той же вакансии обновляет запись."""
    company = None
    if card.company_slug:
        company, company_created = Company.objects.get_or_create(
            slug=card.company_slug,
            defaults={"name": card.company_name or card.company_slug, "linkedin_url": card.company_url},
        )
        if company_created:
            run.companies_created += 1
        elif card.company_name and company.name != card.company_name and not company.details_fetched_at:
            company.name = card.company_name
            company.save(update_fields=["name", "updated_at"])

    defaults = {
        "title": card.title[:500],
        "company": company,
        "company_name_raw": card.company_name[:255],
        "location": card.location[:255],
        "url": card.url[:1000],
        "posted_raw": card.posted_raw[:255],
        "posted_at": card.posted_at,
        "is_reposted": card.is_reposted,
        "benefits": card.benefits[:500],
        "first_run": run,
        "last_run": run,
        "last_task": task,
    }

    vacancy, created = Vacancy.objects.get_or_create(job_id=card.job_id, defaults=defaults)

    if not created:
        vacancy.title = card.title[:500] or vacancy.title
        vacancy.location = card.location[:255] or vacancy.location
        vacancy.posted_raw = card.posted_raw[:255] or vacancy.posted_raw
        vacancy.posted_at = card.posted_at or vacancy.posted_at
        vacancy.is_reposted = card.is_reposted or vacancy.is_reposted
        vacancy.benefits = card.benefits[:500] or vacancy.benefits
        vacancy.last_run = run
        vacancy.last_task = task
        vacancy.times_seen += 1
        if company and vacancy.company_id is None:
            vacancy.company = company
        vacancy.save(
            update_fields=[
                "title",
                "location",
                "posted_raw",
                "posted_at",
                "is_reposted",
                "benefits",
                "last_run",
                "last_task",
                "times_seen",
                "company",
                "updated_at",
            ]
        )

    vacancy.search_queries.add(query)
    needs_detail = vacancy.status != VacancyStatus.DETAILED
    return vacancy, created, needs_detail


# ----------------------------------------------------------------- догрузка

def _backfill(run: ParseRun, query: SearchQuery, client: LinkedInGuestClient, budget: _Budget) -> None:
    """Список не обходим - добираем недостающее по уже собранным вакансиям."""
    run.append_log("Режим догрузки: список не трогаю, добираю недостающее.", save=False)
    _save_counters(run)

    if query.fetch_descriptions:
        pending = list(
            Vacancy.objects.filter(search_queries=query)
            .exclude(status=VacancyStatus.DETAILED)
            .values_list("pk", flat=True)
        )
        run.append_log(f"Описания: в очереди {len(pending)} вакансий.", save=False)
        _save_counters(run)
        _process_details(run, client, budget, pending, wait_on_pause=True)
    if query.fetch_companies:
        _process_companies(run, client, budget, query=query)


# ------------------------------------------------------- обработка пачками

def _wait_out_pause(run: ParseRun, is_off) -> bool:
    """Пережидает паузу фазы, не теряя отзывчивости на «Остановить».

    Нужно в режиме догрузки: там фаза - это весь смысл запуска, и выходить
    из неё по первой же паузе бессмысленно. В обычном обходе ждать не нужно:
    впереди ещё сотни страниц, пауза истечёт сама собой.
    """
    for _ in range(360):  # максимум 30 минут ожидания
        _check_stop(run)
        if not is_off():
            return True
        time.sleep(5)
    return not is_off()


def _process_details(
    run: ParseRun,
    client: LinkedInGuestClient,
    budget: _Budget,
    vacancy_ids: list[int],
    *,
    wait_on_pause: bool = False,
) -> None:
    for vacancy_id in vacancy_ids:
        if budget.details_off:
            if not (wait_on_pause and _wait_out_pause(run, lambda: budget.details_off)):
                return
        _check_stop(run)
        vacancy = Vacancy.objects.filter(pk=vacancy_id).first()
        if not vacancy or vacancy.status == VacancyStatus.DETAILED:
            continue
        budget.note_detail(fetch_detail(run, client, vacancy))
        _save_counters(run)


def _process_companies(
    run: ParseRun,
    client: LinkedInGuestClient,
    budget: _Budget,
    *,
    vacancy_ids: list[int] | None = None,
    query: SearchQuery | None = None,
) -> None:
    """Догружает профили компаний без профиля.

    vacancy_ids - компании вакансий текущей страницы (обычный обход);
    query - все компании вакансий запроса (режим догрузки).
    """
    scope = Company.objects.filter(details_fetched_at__isnull=True)
    if vacancy_ids is not None:
        scope = scope.filter(vacancies__pk__in=vacancy_ids)
    elif query is not None:
        scope = scope.filter(vacancies__search_queries=query)
    else:
        return

    company_ids = list(scope.distinct().values_list("pk", flat=True))
    if not company_ids:
        return
    if query is not None:
        run.append_log(f"Компании: в очереди {len(company_ids)} профилей.", save=False)
        _save_counters(run)

    wait_on_pause = query is not None  # режим догрузки
    for company_id in company_ids:
        if budget.companies_off:
            if not (wait_on_pause and _wait_out_pause(run, lambda: budget.companies_off)):
                return
        _check_stop(run)
        company = Company.objects.filter(pk=company_id).first()
        if not company or company.details_fetched_at:
            continue
        budget.note_company(fetch_company(run, client, company))
        _save_counters(run)


# -------------------------------------------------- загрузка одной сущности

def fetch_detail(run: ParseRun | None, client: LinkedInGuestClient, vacancy: Vacancy) -> str:
    """Грузит описание одной вакансии. Возвращает "ok" | "failed" | "blocked".

    Используется и из запуска, и из админки (повторная загрузка).
    """
    vacancy.detail_attempts += 1
    try:
        detail = client.job_detail(vacancy.job_id)
    except Blocked as exc:
        # Статус оставляем прежним: это не «вакансия битая», а нас придержали.
        vacancy.detail_error = str(exc)[:2000]
        vacancy.save(update_fields=["detail_error", "detail_attempts", "updated_at"])
        if run:
            run.detail_requests += 1
            run.errors_count += 1
            run.append_log(f"Описание {vacancy.job_id}: {exc}", save=False)
        return "blocked"
    except ScraperError as exc:
        vacancy.status = VacancyStatus.FAILED
        vacancy.detail_error = str(exc)[:2000]
        vacancy.save(update_fields=["status", "detail_error", "detail_attempts", "updated_at"])
        if run:
            run.detail_requests += 1
            run.errors_count += 1
            run.append_log(f"Описание {vacancy.job_id}: {exc}", save=False)
        return "failed"

    vacancy.description = detail.description
    vacancy.description_html = detail.description_html
    vacancy.applicants_raw = detail.applicants_raw[:255]
    vacancy.seniority = detail.seniority[:255]
    vacancy.employment_type = detail.employment_type[:255]
    vacancy.job_function = detail.job_function[:255]
    vacancy.industries = detail.industries[:500]
    if detail.location and not vacancy.location:
        vacancy.location = detail.location[:255]
    vacancy.status = VacancyStatus.DETAILED
    vacancy.detail_error = ""
    vacancy.details_fetched_at = timezone.now()

    if vacancy.company_id is None and detail.company_url:
        from .parser import _company_slug

        slug = _company_slug(detail.company_url)
        if slug:
            company, created = Company.objects.get_or_create(
                slug=slug,
                defaults={"name": detail.company_name or slug, "linkedin_url": detail.company_url},
            )
            vacancy.company = company
            if created and run:
                run.companies_created += 1

    vacancy.save()
    if run:
        run.detail_requests += 1
    return "ok"


def fetch_company(run: ParseRun | None, client: LinkedInGuestClient, company: Company) -> str:
    """Возвращает "ok" | "failed" | "blocked"."""
    try:
        profile = client.company_profile(company.slug)
    except Blocked as exc:
        # Временный рейт-лимит. details_fetched_at НЕ ставим: компания должна
        # остаться в очереди и подобраться следующей догрузкой.
        company.fetch_error = str(exc)[:2000]
        company.save(update_fields=["fetch_error", "updated_at"])
        if run:
            run.company_requests += 1
            run.errors_count += 1
            run.append_log(f"Компания {company.slug}: {exc}", save=False)
        return "blocked"
    except ScraperError as exc:
        company.fetch_error = str(exc)[:2000]
        company.details_fetched_at = timezone.now()
        company.save(update_fields=["fetch_error", "details_fetched_at", "updated_at"])
        if run:
            run.company_requests += 1
            run.errors_count += 1
            run.append_log(f"Компания {company.slug}: {exc}", save=False)
        return "failed"

    company.name = profile.name[:255] or company.name
    company.description = profile.description or company.description
    company.tagline = profile.tagline[:500] or company.tagline
    company.industry = profile.industry[:255] or company.industry
    company.company_size = profile.company_size[:255] or company.company_size
    company.headquarters = profile.headquarters[:255] or company.headquarters
    company.website = profile.website[:500] or company.website
    company.followers = profile.followers[:100] or company.followers
    company.logo_url = profile.logo_url[:500] or company.logo_url
    company.linkedin_url = company.linkedin_url or f"https://www.linkedin.com/company/{company.slug}/"
    company.fetch_error = ""
    company.details_fetched_at = timezone.now()
    company.save()
    if run:
        run.company_requests += 1
    return "ok"


def build_client(query: SearchQuery | None = None) -> LinkedInGuestClient:
    """Клиент для разовых операций из админки (повторная загрузка одной записи)."""
    return LinkedInGuestClient(
        user_agent=settings.SCRAPER_USER_AGENT,
        timeout=(query.request_timeout if query else None) or settings.SCRAPER_DEFAULT_TIMEOUT,
        delay=(query.delay_seconds if query else None) or settings.SCRAPER_DEFAULT_DELAY,
        max_retries=settings.SCRAPER_MAX_RETRIES,
    )
