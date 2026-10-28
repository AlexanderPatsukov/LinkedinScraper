from django.db import models
from django.utils import timezone

from .countries import COUNTRY_CHOICES, WORLDWIDE


class DatePosted(models.TextChoices):
    ANY = "", "За всё время"
    DAY = "r86400", "За сутки"
    WEEK = "r604800", "За неделю"
    MONTH = "r2592000", "За месяц"


# Формат работы (f_WT), уровень (f_E) и тип занятости (f_JT) здесь намеренно
# отсутствуют: гостевой эндпоинт принимает эти параметры молча и возвращает
# ровно ту же выдачу (проверено сравнением job_id - пересечение 10 из 10 при
# любом значении). Работают только location и f_TPR.


class Company(models.Model):
    # CharField, а не SlugField: встречаются слаги с точками и процент-кодированием
    slug = models.CharField("LinkedIn slug", max_length=255, unique=True)
    name = models.CharField("Название", max_length=255, db_index=True)
    linkedin_url = models.URLField("Ссылка на компанию", max_length=500, blank=True)
    description = models.TextField("Описание компании", blank=True)
    tagline = models.CharField("Короткое описание", max_length=500, blank=True)
    industry = models.CharField("Отрасль", max_length=255, blank=True)
    company_size = models.CharField("Размер", max_length=255, blank=True)
    headquarters = models.CharField("Штаб-квартира", max_length=255, blank=True)
    website = models.URLField("Сайт", max_length=500, blank=True)
    followers = models.CharField("Подписчики", max_length=100, blank=True)
    logo_url = models.URLField("Логотип", max_length=500, blank=True)

    details_fetched_at = models.DateTimeField("Профиль загружен", null=True, blank=True)
    fetch_error = models.TextField("Ошибка загрузки профиля", blank=True)
    created_at = models.DateTimeField("Создана", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлена", auto_now=True)

    class Meta:
        verbose_name = "Компания"
        verbose_name_plural = "Компании"
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def has_details(self) -> bool:
        return bool(self.details_fetched_at and not self.fetch_error)


class SearchQuery(models.Model):
    """Поисковый запрос: что и где искать. Запросов может быть сколько угодно."""

    name = models.CharField(
        "Название",
        max_length=255,
        blank=True,
        help_text="Необязательно. Если пусто - соберётся из запроса и страны.",
    )
    keywords = models.CharField("Поисковый запрос", max_length=255, help_text='Например: "Python Developer"')
    country = models.CharField("Страна", max_length=100, choices=COUNTRY_CHOICES, default="Ukraine")
    geo_id = models.CharField(
        "geoId (переопределение)",
        max_length=50,
        blank=True,
        help_text="Необязательно. Если указан - используется вместо названия страны "
        "(нужно для поиска по конкретному городу или региону).",
    )

    date_posted = models.CharField("Дата публикации", max_length=20, choices=DatePosted.choices, blank=True, default="")

    max_requests = models.PositiveIntegerField(
        "Макс. запросов списка за запуск",
        default=10,
        help_text="Общий бюджет запросов к спискам на весь запуск, включая все задачи дробления. "
        "1 запрос = 10 вакансий. 0 - без лимита.",
    )
    request_timeout = models.PositiveIntegerField("Таймаут запроса, сек", default=20)
    delay_seconds = models.FloatField(
        "Пауза между запросами, сек",
        default=2.0,
        help_text="Меньше 1 сек ставить не стоит - словите 429.",
    )
    fetch_descriptions = models.BooleanField(
        "Грузить описания вакансий",
        default=True,
        help_text="Отдельный запрос на каждую новую вакансию.",
    )
    fetch_companies = models.BooleanField(
        "Грузить профили компаний",
        default=True,
        help_text="Отдельный запрос на каждую новую компанию (результат кэшируется).",
    )
    max_company_profiles = models.PositiveIntegerField(
        "Макс. профилей компаний за запуск",
        default=0,
        help_text="0 - без лимита. Страницы компаний LinkedIn лимитирует жёстче остальных, "
        "поэтому это самая медленная часть сбора. Поставьте небольшое число, если "
        "вакансии нужнее профилей: названия и ссылки компаний собираются всегда, "
        "лимит касается только подробных профилей.",
    )

    split_enabled = models.BooleanField(
        "Дробить при упоре в потолок",
        default=True,
        help_text="LinkedIn отдаёт максимум 1000 результатов на одну комбинацию параметров "
        "(start=1000 возвращает HTTP 400). Если задача упёрлась в потолок, она делится "
        "на подзадачи по локациям, затем по датам, затем по уточняющим словам - "
        "и каждая подзадача получает свою тысячу.",
    )
    max_depth = models.PositiveSmallIntegerField(
        "Макс. глубина дробления",
        default=3,
        help_text="Сколько раз подряд задача может делиться. 0 - не дробить.",
    )
    max_tasks = models.PositiveIntegerField(
        "Макс. задач на запрос",
        default=200,
        help_text="Предохранитель от разрастания дерева. 0 - без лимита.",
    )
    split_locations_limit = models.PositiveSmallIntegerField(
        "Локаций на одно деление",
        default=25,
        help_text="Сколько городов/областей брать при делении по географии. "
        "Берутся самые частые из уже собранных вакансий.",
    )
    split_keywords = models.CharField(
        "Уточняющие слова для дробления",
        max_length=500,
        blank=True,
        help_text="Через запятую, например: django, fastapi, flask, senior, junior. "
        "Используются на последней оси дробления: к запросу дописывается по одному слову. "
        "Пусто - ось не используется.",
    )

    is_active = models.BooleanField("Активен", default=True)
    created_at = models.DateTimeField("Создан", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлён", auto_now=True)

    class Meta:
        verbose_name = "Поисковый запрос"
        verbose_name_plural = "Поисковые запросы"
        ordering = ["-created_at"]

    def __str__(self):
        return self.title

    @property
    def title(self) -> str:
        if self.name:
            return self.name
        return f"{self.keywords} - {self.country}"

    def save(self, *args, **kwargs):
        if not self.name:
            self.name = f"{self.keywords} - {self.country}"
        super().save(*args, **kwargs)

    @property
    def root_location(self) -> str:
        """Локация корневой задачи: страна, либо пусто при поиске по geoId."""
        if self.geo_id or self.country == WORLDWIDE:
            return ""
        return self.country

    def split_keyword_list(self) -> list[str]:
        return [word.strip() for word in self.split_keywords.split(",") if word.strip()]


class RunStatus(models.TextChoices):
    PENDING = "pending", "В очереди"
    RUNNING = "running", "Выполняется"
    STOPPING = "stopping", "Останавливается"
    DONE = "done", "Завершён"
    STOPPED = "stopped", "Остановлен"
    FAILED = "failed", "Ошибка"


class ParseRun(models.Model):
    """Один запуск парсера по конкретному поисковому запросу."""

    search_query = models.ForeignKey(
        SearchQuery, verbose_name="Поисковый запрос", on_delete=models.CASCADE, related_name="runs"
    )
    status = models.CharField("Статус", max_length=20, choices=RunStatus.choices, default=RunStatus.PENDING, db_index=True)
    stop_requested = models.BooleanField("Запрошена остановка", default=False)
    backfill_only = models.BooleanField(
        "Только догрузка",
        default=False,
        help_text="Список не обходится - добираются недостающие описания и профили компаний.",
    )

    requests_made = models.PositiveIntegerField("Запросов списка", default=0)
    detail_requests = models.PositiveIntegerField("Запросов описаний", default=0)
    company_requests = models.PositiveIntegerField("Запросов компаний", default=0)
    jobs_seen = models.PositiveIntegerField("Карточек найдено", default=0)
    jobs_created = models.PositiveIntegerField("Новых вакансий", default=0)
    jobs_updated = models.PositiveIntegerField("Обновлено вакансий", default=0)
    companies_created = models.PositiveIntegerField("Новых компаний", default=0)
    errors_count = models.PositiveIntegerField("Ошибок", default=0)

    error_message = models.TextField("Текст ошибки", blank=True)
    log = models.TextField("Лог", blank=True)

    started_at = models.DateTimeField("Старт", null=True, blank=True)
    finished_at = models.DateTimeField("Финиш", null=True, blank=True)
    created_at = models.DateTimeField("Создан", auto_now_add=True)

    class Meta:
        verbose_name = "Запуск парсера"
        verbose_name_plural = "Запуски парсера"
        ordering = ["-created_at"]

    def __str__(self):
        return f"#{self.pk} {self.search_query.title} [{self.get_status_display()}]"

    @property
    def is_live(self) -> bool:
        return self.status in {RunStatus.PENDING, RunStatus.RUNNING, RunStatus.STOPPING}

    @property
    def duration(self):
        if not self.started_at:
            return None
        return (self.finished_at or timezone.now()) - self.started_at

    def append_log(self, line: str, *, save: bool = True) -> None:
        stamp = timezone.localtime().strftime("%H:%M:%S")
        self.log = f"{self.log}[{stamp}] {line}\n"
        if save:
            self.save(update_fields=["log"])


class VacancyStatus(models.TextChoices):
    NEW = "new", "Только из списка"
    DETAILED = "detailed", "Описание загружено"
    FAILED = "failed", "Ошибка загрузки"


class Vacancy(models.Model):
    job_id = models.CharField("LinkedIn job ID", max_length=50, unique=True, db_index=True)
    title = models.CharField("Вакансия", max_length=500, db_index=True)
    company = models.ForeignKey(
        Company,
        verbose_name="Компания",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="vacancies",
    )
    company_name_raw = models.CharField("Компания (из карточки)", max_length=255, blank=True)

    location = models.CharField("Локация", max_length=255, blank=True, db_index=True)
    url = models.URLField("Ссылка на вакансию", max_length=1000, blank=True)

    posted_raw = models.CharField("Размещена (текст)", max_length=255, blank=True)
    posted_at = models.DateField("Дата размещения", null=True, blank=True, db_index=True)
    is_reposted = models.BooleanField("Размещена повторно", default=False)
    applicants_raw = models.CharField("Откликов", max_length=255, blank=True)
    benefits = models.CharField("Плюшки", max_length=500, blank=True)

    description = models.TextField("Описание вакансии", blank=True)
    description_html = models.TextField("Описание (HTML)", blank=True)
    seniority = models.CharField("Уровень", max_length=255, blank=True)
    employment_type = models.CharField("Тип занятости", max_length=255, blank=True)
    job_function = models.CharField("Функция", max_length=255, blank=True)
    industries = models.CharField("Отрасли", max_length=500, blank=True)

    status = models.CharField(
        "Статус обработки", max_length=20, choices=VacancyStatus.choices, default=VacancyStatus.NEW, db_index=True
    )
    detail_error = models.TextField("Ошибка обработки", blank=True)
    detail_attempts = models.PositiveIntegerField("Попыток загрузки", default=0)
    details_fetched_at = models.DateTimeField("Описание загружено", null=True, blank=True)

    search_queries = models.ManyToManyField(
        SearchQuery, verbose_name="Найдена по запросам", related_name="vacancies", blank=True
    )
    first_run = models.ForeignKey(
        ParseRun,
        verbose_name="Первый запуск",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="first_seen_vacancies",
    )
    last_run = models.ForeignKey(
        ParseRun,
        verbose_name="Последний запуск",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="last_seen_vacancies",
    )
    last_task = models.ForeignKey(
        "CrawlTask",
        verbose_name="Найдена задачей",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="found_vacancies",
        help_text="Нужна для дробления: локации подзадач берутся из вакансий, "
        "которые нашла именно эта задача.",
    )

    times_seen = models.PositiveIntegerField("Раз встречена", default=1)
    created_at = models.DateTimeField("Добавлена", auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField("Обновлена", auto_now=True)

    class Meta:
        verbose_name = "Вакансия"
        verbose_name_plural = "Вакансии"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["company", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.title} - {self.company_name_raw or (self.company.name if self.company else '')}"


class TaskStatus(models.TextChoices):
    PENDING = "pending", "В очереди"
    RUNNING = "running", "Выполняется"
    DONE = "done", "Пройдена"
    SATURATED = "saturated", "Упёрлась в потолок"
    FAILED = "failed", "Ошибка"


class SplitAxis(models.TextChoices):
    ROOT = "root", "Корень"
    LOCATION = "location", "По локации"
    DATE = "date", "По дате"
    KEYWORDS = "keywords", "По словам"


class CrawlTask(models.Model):
    """Одна комбинация фильтров = одна задача со своим лимитом в 1000 результатов.

    Дерево задач и есть «место, на котором мы остановились»: статусы лежат в БД,
    поэтому остановка сохраняет фронтир, а продолжение - это просто разбор
    очереди дальше. Никакой позиции запоминать не нужно: выдача LinkedIn
    нестабильна, а дедуп по job_id делает повторные проходы безвредными.
    """

    search_query = models.ForeignKey(
        SearchQuery, verbose_name="Поисковый запрос", on_delete=models.CASCADE, related_name="tasks"
    )
    parent = models.ForeignKey(
        "self", verbose_name="Родительская задача", on_delete=models.CASCADE, null=True, blank=True, related_name="children"
    )
    depth = models.PositiveSmallIntegerField("Глубина", default=0)
    split_axis = models.CharField("Получена делением", max_length=20, choices=SplitAxis.choices, default=SplitAxis.ROOT)

    keywords = models.CharField("Запрос", max_length=255)
    location = models.CharField("Локация", max_length=255, blank=True)
    geo_id = models.CharField("geoId", max_length=50, blank=True)
    date_posted = models.CharField("Дата публикации", max_length=20, blank=True)

    status = models.CharField("Статус", max_length=20, choices=TaskStatus.choices, default=TaskStatus.PENDING, db_index=True)
    pages_done = models.PositiveIntegerField(
        "Пройдено страниц",
        default=0,
        help_text="Это и позиция в выдаче: прерванная задача продолжится с этой страницы, "
        "а не с начала. Действие «Вернуть в очередь» сбрасывает её в ноль.",
    )
    found_count = models.PositiveIntegerField("Карточек найдено", default=0)
    new_count = models.PositiveIntegerField("Новых вакансий", default=0)
    note = models.TextField("Заметка", blank=True)

    last_run = models.ForeignKey(
        ParseRun, verbose_name="Последний запуск", on_delete=models.SET_NULL, null=True, blank=True, related_name="tasks"
    )
    created_at = models.DateTimeField("Создана", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлена", auto_now=True)
    finished_at = models.DateTimeField("Завершена", null=True, blank=True)

    class Meta:
        verbose_name = "Задача сбора"
        verbose_name_plural = "Задачи сбора"
        ordering = ["depth", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["search_query", "keywords", "location", "geo_id", "date_posted"],
                name="uniq_crawl_task_facets",
            )
        ]
        indexes = [models.Index(fields=["search_query", "status"])]

    def __str__(self):
        return self.label

    @property
    def label(self) -> str:
        parts = [self.keywords]
        if self.location:
            parts.append(self.location)
        elif self.geo_id:
            parts.append(f"geoId={self.geo_id}")
        if self.date_posted:
            parts.append(dict(DatePosted.choices).get(self.date_posted, self.date_posted))
        return " / ".join(parts)

    def build_params(self, start: int = 0) -> dict:
        params = {"keywords": self.keywords, "start": start}
        if self.geo_id:
            params["geoId"] = self.geo_id
        elif self.location:
            params["location"] = self.location
        if self.date_posted:
            params["f_TPR"] = self.date_posted
        return params

    def axes_used(self) -> set[str]:
        """Оси, по которым уже делились предки этой задачи (включая её саму)."""
        used, node = set(), self
        while node is not None:
            if node.split_axis != SplitAxis.ROOT:
                used.add(node.split_axis)
            node = node.parent
        return used
