from django.contrib import admin, messages
from django.db.models import Count
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect
from django.urls import path, reverse
from django.utils.html import format_html, format_html_join
from django.utils.http import urlencode

from . import exports, runner
from .models import (
    Company,
    CrawlTask,
    ParseRun,
    RunStatus,
    SearchQuery,
    SplitAxis,
    TaskStatus,
    Vacancy,
    VacancyStatus,
)

admin.site.site_header = "LinkedIn Jobs - парсер вакансий"
admin.site.site_title = "LinkedIn Jobs"
admin.site.index_title = "Разделы"

# Порядок разделов на главной: сначала два основных, потом справочные.
_MODEL_ORDER = {"SearchQuery": 1, "Vacancy": 2, "Company": 3, "CrawlTask": 4, "ParseRun": 5}
_original_get_app_list = admin.site.get_app_list


def _ordered_app_list(request, app_label=None):
    app_list = _original_get_app_list(request, app_label)
    for app in app_list:
        if app["app_label"] == "jobs":
            app["models"].sort(key=lambda model: _MODEL_ORDER.get(model["object_name"], 99))
    return app_list


admin.site.get_app_list = _ordered_app_list


STATUS_COLORS = {
    RunStatus.PENDING: "#6b7280",
    RunStatus.RUNNING: "#2563eb",
    RunStatus.STOPPING: "#d97706",
    RunStatus.DONE: "#15803d",
    RunStatus.STOPPED: "#6b7280",
    RunStatus.FAILED: "#b91c1c",
    VacancyStatus.NEW: "#6b7280",
    VacancyStatus.DETAILED: "#15803d",
    VacancyStatus.FAILED: "#b91c1c",
    TaskStatus.PENDING: "#6b7280",
    TaskStatus.RUNNING: "#2563eb",
    TaskStatus.DONE: "#15803d",
    TaskStatus.SATURATED: "#7c3aed",
    TaskStatus.FAILED: "#b91c1c",
}


def _badge(value: str, label: str) -> str:
    return format_html(
        '<span style="background:{};color:#fff;padding:2px 8px;border-radius:10px;'
        'font-size:11px;white-space:nowrap">{}</span>',
        STATUS_COLORS.get(value, "#6b7280"),
        label,
    )


def _changelist_link(model: str, label: str, **params) -> str:
    url = reverse(f"admin:jobs_{model}_changelist")
    if params:
        url = f"{url}?{urlencode(params)}"
    return format_html('<a href="{}">{}</a>', url, label)


# --------------------------------------------------------------- 1. Запросы

class ParseRunInline(admin.TabularInline):
    model = ParseRun
    extra = 0
    can_delete = False
    show_change_link = True
    fields = ("status_badge", "requests_made", "jobs_created", "jobs_updated", "errors_count", "started_at", "finished_at")
    readonly_fields = fields
    ordering = ("-created_at",)
    verbose_name = "Запуск"
    verbose_name_plural = "История запусков"

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description="Статус")
    def status_badge(self, obj):
        return _badge(obj.status, obj.get_status_display())


@admin.register(SearchQuery)
class SearchQueryAdmin(admin.ModelAdmin):
    list_display = (
        "title_col",
        "keywords",
        "country",
        "limits_col",
        "is_active",
        "last_run_col",
        "tasks_col",
        "vacancies_col",
        "run_button",
    )
    list_filter = ("is_active", "country", "date_posted", "split_enabled")
    search_fields = ("name", "keywords", "country", "geo_id")
    list_per_page = 50
    inlines = [ParseRunInline]
    actions = ["action_run", "action_backfill", "action_reset_tasks", "action_activate", "action_deactivate"]
    fieldsets = (
        ("Что искать", {"fields": ("name", "keywords", "is_active")}),
        ("Где искать", {"fields": ("country", "geo_id")}),
        (
            "Фильтр LinkedIn",
            {
                "fields": ("date_posted",),
                "description": "Формат работы, уровень и тип занятости здесь отсутствуют намеренно: "
                "гостевой эндпоинт LinkedIn принимает эти параметры, но возвращает ту же самую "
                "выдачу. Реально фильтруют только локация и дата публикации.",
            },
        ),
        (
            "Обход потолка в 1000 результатов",
            {
                "fields": (
                    "split_enabled",
                    "max_depth",
                    "max_tasks",
                    "split_locations_limit",
                    "split_keywords",
                ),
                "description": "LinkedIn отдаёт максимум 1000 результатов на одну комбинацию параметров. "
                "Задача, упёршаяся в потолок, делится на подзадачи - сначала по локациям "
                "(берутся из уже собранных вакансий), затем по датам, затем по уточняющим словам. "
                "Каждая подзадача получает свою тысячу.",
            },
        ),
        (
            "Параметры парсинга",
            {
                "fields": (
                    "max_requests",
                    "request_timeout",
                    "delay_seconds",
                    "fetch_descriptions",
                    "fetch_companies",
                    "max_company_profiles",
                )
            },
        ),
    )

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .annotate(vacancy_total=Count("vacancies", distinct=True))
            .prefetch_related("runs")
        )

    @admin.display(description="Запрос", ordering="name")
    def title_col(self, obj):
        return obj.title

    @admin.display(description="Лимиты")
    def limits_col(self, obj):
        limit = "без лимита" if obj.max_requests == 0 else f"{obj.max_requests} зап. × 10"
        if not obj.fetch_companies:
            companies = "без профилей компаний"
        elif obj.max_company_profiles:
            companies = f"компаний ≤{obj.max_company_profiles}"
        else:
            companies = "компании все"
        return f"{limit} · {companies} · пауза {obj.delay_seconds:g}с · таймаут {obj.request_timeout}с"

    @admin.display(description="Последний запуск")
    def last_run_col(self, obj):
        run = obj.runs.all().first()
        if not run:
            return "-"
        url = reverse("admin:jobs_parserun_change", args=[run.pk])
        return format_html(
            '<a href="{}">#{}</a> {}', url, run.pk, _badge(run.status, run.get_status_display())
        )

    @admin.display(description="Вакансий", ordering="vacancy_total")
    def vacancies_col(self, obj):
        return _changelist_link("vacancy", obj.vacancy_total, search_queries__id__exact=obj.pk)

    @admin.display(description="Задачи")
    def tasks_col(self, obj):
        counts = {row["status"]: row["n"] for row in obj.tasks.values("status").annotate(n=Count("pk"))}
        total = sum(counts.values())
        if not total:
            return _changelist_link("crawltask", "нет", search_query__id__exact=obj.pk)
        pending = counts.get(TaskStatus.PENDING, 0)
        saturated = counts.get(TaskStatus.SATURATED, 0)
        text = f"{total}"
        if pending:
            text += f", в очереди {pending}"
        if saturated:
            text += f", упёрлось {saturated}"
        return _changelist_link("crawltask", text, search_query__id__exact=obj.pk)

    @admin.display(description="Действие")
    def run_button(self, obj):
        url = reverse("admin:jobs_searchquery_run", args=[obj.pk])
        return format_html(
            '<a class="button" style="background:#2563eb;color:#fff;padding:4px 10px;'
            'border-radius:4px;white-space:nowrap" href="{}">▶ Запустить</a>',
            url,
        )

    def get_urls(self):
        return [
            path(
                "<int:pk>/run/",
                self.admin_site.admin_view(self.run_view),
                name="jobs_searchquery_run",
            ),
        ] + super().get_urls()

    def run_view(self, request, pk):
        query = get_object_or_404(SearchQuery, pk=pk)
        if not self.has_change_permission(request, query):
            self.message_user(request, "Недостаточно прав.", messages.ERROR)
            return redirect("admin:jobs_searchquery_changelist")
        run, error = runner.start_run(query)
        if error:
            self.message_user(request, error, messages.WARNING)
            return redirect("admin:jobs_parserun_changelist")
        self.message_user(request, f"Запуск #{run.pk} стартовал: {query.title}", messages.SUCCESS)
        return redirect("admin:jobs_parserun_change", run.pk)

    @admin.action(description="▶ Запустить парсинг")
    def action_run(self, request, queryset):
        started, skipped = [], []
        for query in queryset:
            run, error = runner.start_run(query)
            (skipped if error else started).append(error or f"#{run.pk} {query.title}")
        if started:
            self.message_user(request, "Запущено: " + "; ".join(started), messages.SUCCESS)
        for note in skipped:
            self.message_user(request, note, messages.WARNING)

    @admin.action(description="⤓ Догрузить недостающие описания и компании")
    def action_backfill(self, request, queryset):
        started, skipped = [], []
        for query in queryset:
            run, error = runner.start_run(query, backfill_only=True)
            (skipped if error else started).append(error or f"#{run.pk} {query.title}")
        if started:
            self.message_user(
                request,
                "Догрузка запущена (список не обходится): " + "; ".join(started),
                messages.SUCCESS,
            )
        for note in skipped:
            self.message_user(request, note, messages.WARNING)

    @admin.action(description="✖ Сбросить дерево задач")
    def action_reset_tasks(self, request, queryset):
        live = ParseRun.objects.filter(
            search_query__in=queryset,
            status__in=[RunStatus.PENDING, RunStatus.RUNNING, RunStatus.STOPPING],
        ).exists()
        if live:
            self.message_user(
                request,
                "Сначала остановите выполняющиеся запуски - иначе задачи пересоздадутся на ходу.",
                messages.WARNING,
            )
            return
        deleted, _ = CrawlTask.objects.filter(search_query__in=queryset).delete()
        self.message_user(
            request,
            f"Удалено задач: {deleted}. Дерево построится заново при следующем запуске "
            "(собранные вакансии остаются на месте).",
            messages.SUCCESS,
        )

    @admin.action(description="Активировать")
    def action_activate(self, request, queryset):
        updated = queryset.update(is_active=True)
        self.message_user(request, f"Активировано: {updated}")

    @admin.action(description="Деактивировать")
    def action_deactivate(self, request, queryset):
        updated = queryset.update(is_active=False)
        self.message_user(request, f"Деактивировано: {updated}")


# -------------------------------------------------------------- 2. Вакансии

class HasDescriptionFilter(admin.SimpleListFilter):
    title = "описание"
    parameter_name = "has_description"

    def lookups(self, request, model_admin):
        return (("yes", "Есть"), ("no", "Нет"))

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.exclude(description="")
        if self.value() == "no":
            return queryset.filter(description="")
        return queryset


@admin.register(Vacancy)
class VacancyAdmin(admin.ModelAdmin):
    list_display = ("title_col", "company_col", "location", "posted_col", "applicants_raw", "status_badge")
    list_display_links = ("title_col",)
    list_filter = (
        "status",
        HasDescriptionFilter,
        "is_reposted",
        "search_queries__country",
        "search_queries",
        "employment_type",
        "seniority",
        "posted_at",
    )
    search_fields = (
        "title",
        "company__name",
        "company_name_raw",
        "company__slug",
        "location",
        "description",
        "job_id",
    )
    search_help_text = (
        "Поиск по названию вакансии, компании, локации, тексту описания и job ID. "
        "Чтобы увидеть смежные вакансии компании - кликните по её названию в колонке «Компания»."
    )
    list_select_related = ("company",)
    list_per_page = 50
    date_hierarchy = "created_at"
    actions = ["action_export_xlsx", "action_export_csv", "action_retry_details"]
    autocomplete_fields = ("company",)
    filter_horizontal = ("search_queries",)
    readonly_fields = (
        "job_id",
        "times_seen",
        "detail_attempts",
        "details_fetched_at",
        "created_at",
        "updated_at",
        "first_run",
        "last_run",
        "last_task",
        "company_card",
        "description_preview",
        "open_link",
    )
    fieldsets = (
        (
            "Вакансия",
            {
                "fields": (
                    "title",
                    "job_id",
                    "open_link",
                    "location",
                    ("posted_raw", "posted_at", "is_reposted"),
                    "applicants_raw",
                    "benefits",
                )
            },
        ),
        ("Компания", {"fields": ("company", "company_name_raw", "company_card")}),
        ("Классификация", {"fields": ("employment_type", "seniority", "job_function", "industries")}),
        ("Описание", {"fields": ("description_preview", "description")}),
        (
            "Обработка",
            {
                "fields": (
                    "status",
                    "detail_error",
                    "detail_attempts",
                    "details_fetched_at",
                    "search_queries",
                    ("first_run", "last_run"),
                    "last_task",
                    ("times_seen", "created_at", "updated_at"),
                )
            },
        ),
        ("HTML-описание (как есть)", {"classes": ("collapse",), "fields": ("description_html",)}),
    )

    @admin.display(description="Вакансия", ordering="title")
    def title_col(self, obj):
        return obj.title

    @admin.display(description="Компания", ordering="company__name")
    def company_col(self, obj):
        if not obj.company:
            return obj.company_name_raw or "-"
        count = obj.company.vacancies.count()
        return format_html(
            "{} <span style=\"color:#6b7280\">({})</span>",
            _changelist_link("vacancy", obj.company.name, company__id__exact=obj.company_id),
            count,
        )

    @admin.display(description="Размещена", ordering="posted_at")
    def posted_col(self, obj):
        text = obj.posted_raw or (obj.posted_at.isoformat() if obj.posted_at else "-")
        if obj.is_reposted:
            return format_html("{} <span style=\"color:#d97706\">↻</span>", text)
        return text

    @admin.display(description="Статус", ordering="status")
    def status_badge(self, obj):
        return _badge(obj.status, obj.get_status_display())

    @admin.display(description="Ссылка")
    def open_link(self, obj):
        if not obj.url:
            return "-"
        return format_html('<a href="{}" target="_blank" rel="noopener">Открыть на LinkedIn ↗</a>', obj.url)

    @admin.display(description="Карточка компании")
    def company_card(self, obj):
        company = obj.company
        if not company:
            return "-"
        rows = [
            ("Отрасль", company.industry),
            ("Размер", company.company_size),
            ("Штаб-квартира", company.headquarters),
            ("Подписчики", company.followers),
            ("Сайт", company.website),
        ]
        body = format_html_join(
            "",
            '<tr><th style="text-align:left;padding:2px 12px 2px 0;color:#6b7280;font-weight:400">{}</th>'
            "<td>{}</td></tr>",
            ((label, value) for label, value in rows if value),
        )
        link = _changelist_link("vacancy", "Все вакансии компании →", company__id__exact=company.pk)
        return format_html(
            '<div><a href="{}"><b>{}</b></a> · {}<table style="margin-top:6px">{}</table>'
            '<div style="margin-top:6px;max-width:70em;white-space:pre-wrap">{}</div></div>',
            reverse("admin:jobs_company_change", args=[company.pk]),
            company.name,
            link,
            body,
            company.description or "- описание не загружено -",
        )

    @admin.display(description="Описание (превью)")
    def description_preview(self, obj):
        if not obj.description:
            return "-"
        return format_html(
            '<div style="max-width:70em;max-height:28em;overflow:auto;white-space:pre-wrap;'
            'border:1px solid #e5e7eb;padding:10px;border-radius:6px">{}</div>',
            obj.description,
        )

    @admin.action(description="⬇ Выгрузить в Excel (.xlsx)")
    def action_export_xlsx(self, request, queryset):
        return exports.export_xlsx(queryset)

    @admin.action(description="⬇ Выгрузить в CSV")
    def action_export_csv(self, request, queryset):
        return exports.export_csv(queryset)

    @admin.action(description="↻ Повторно загрузить описания")
    def action_retry_details(self, request, queryset):
        targets = list(queryset.exclude(status=VacancyStatus.DETAILED)[:50])
        if not targets:
            self.message_user(request, "Нечего повторять: у всех выбранных вакансий описание уже есть.", messages.INFO)
            return None
        ok = blocked = 0
        with runner.build_client() as client:
            for vacancy in targets:
                result = runner.fetch_detail(None, client, vacancy)
                ok += result == "ok"
                blocked += result == "blocked"
                if blocked >= runner.CONSECUTIVE_BLOCK_LIMIT:
                    break
        note = f"Загружено описаний: {ok} из {len(targets)}."
        if blocked:
            note += " LinkedIn придержал доступ - остальные остались в очереди, повторите позже."
        elif ok < len(targets):
            note += " Остальные - с ошибкой, см. колонку статуса."
        self.message_user(request, note, messages.SUCCESS if ok else messages.WARNING)
        return None


# --------------------------------------------------------------- 3. Компании

@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ("name", "industry", "company_size", "followers", "vacancies_col", "details_col")
    list_filter = ("industry", "company_size")
    search_fields = ("name", "slug", "industry", "headquarters", "description")
    list_per_page = 50
    actions = ["action_export_xlsx", "action_export_csv", "action_fetch_profiles"]
    readonly_fields = ("slug", "details_fetched_at", "created_at", "updated_at", "logo_preview", "open_link")
    fieldsets = (
        ("Компания", {"fields": ("name", "slug", "linkedin_url", "open_link", "logo_url", "logo_preview")}),
        ("Профиль", {"fields": ("tagline", "description", "industry", "company_size", "headquarters", "website", "followers")}),
        ("Служебное", {"fields": ("fetch_error", "details_fetched_at", "created_at", "updated_at")}),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(vacancy_total=Count("vacancies"))

    @admin.display(description="Вакансий", ordering="vacancy_total")
    def vacancies_col(self, obj):
        return _changelist_link("vacancy", obj.vacancy_total, company__id__exact=obj.pk)

    @admin.display(description="Профиль", boolean=True)
    def details_col(self, obj):
        return obj.has_details

    @admin.display(description="Ссылка")
    def open_link(self, obj):
        url = obj.linkedin_url or f"https://www.linkedin.com/company/{obj.slug}/"
        return format_html('<a href="{}" target="_blank" rel="noopener">Открыть на LinkedIn ↗</a>', url)

    @admin.display(description="Логотип")
    def logo_preview(self, obj):
        if not obj.logo_url:
            return "-"
        return format_html('<img src="{}" style="max-height:80px;border-radius:6px">', obj.logo_url)

    @admin.action(description="⬇ Выгрузить в Excel (.xlsx)")
    def action_export_xlsx(self, request, queryset):
        return exports.export_xlsx(queryset, exports.COMPANIES)

    @admin.action(description="⬇ Выгрузить в CSV")
    def action_export_csv(self, request, queryset):
        return exports.export_csv(queryset, exports.COMPANIES)

    @admin.action(description="↻ Загрузить профили компаний")
    def action_fetch_profiles(self, request, queryset):
        targets = list(queryset[:50])
        ok = blocked = 0
        with runner.build_client() as client:
            for company in targets:
                result = runner.fetch_company(None, client, company)
                ok += result == "ok"
                blocked += result == "blocked"
                if blocked >= runner.CONSECUTIVE_BLOCK_LIMIT:
                    break
        note = f"Загружено профилей: {ok} из {len(targets)}."
        if blocked:
            note += " LinkedIn придержал доступ - остальные остались в очереди, повторите позже."
        self.message_user(request, note, messages.SUCCESS if ok else messages.WARNING)


# ---------------------------------------------------------- 4. Задачи сбора

@admin.register(CrawlTask)
class CrawlTaskAdmin(admin.ModelAdmin):
    list_display = (
        "pk",
        "tree_col",
        "axis_col",
        "status_badge",
        "pages_done",
        "found_count",
        "new_count",
        "vacancies_col",
    )
    list_display_links = ("pk", "tree_col")
    list_filter = ("status", "split_axis", "depth", "search_query")
    search_fields = ("keywords", "location", "geo_id", "note")
    list_select_related = ("search_query", "parent")
    list_per_page = 100
    ordering = ("search_query", "depth", "pk")
    actions = ["action_requeue", "action_mark_done"]
    readonly_fields = (
        "search_query",
        "parent",
        "depth",
        "split_axis",
        "keywords",
        "location",
        "geo_id",
        "date_posted",
        "pages_done",
        "found_count",
        "new_count",
        "last_run",
        "created_at",
        "updated_at",
        "finished_at",
        "params_col",
        "vacancies_col",
    )
    fields = (
        "search_query",
        ("status", "split_axis", "depth"),
        ("keywords", "location", "geo_id", "date_posted"),
        "params_col",
        ("pages_done", "found_count", "new_count"),
        "vacancies_col",
        "parent",
        "last_run",
        "note",
        ("created_at", "updated_at", "finished_at"),
    )

    def has_add_permission(self, request):
        return False

    @admin.display(description="Задача")
    def tree_col(self, obj):
        prefix = "└ " * min(obj.depth, 1) + "· " * max(obj.depth - 1, 0)
        return format_html("<span style=\"color:#6b7280\">{}</span>{}", prefix, obj.label)

    @admin.display(description="Ось", ordering="split_axis")
    def axis_col(self, obj):
        if obj.split_axis == SplitAxis.ROOT:
            return "корень"
        return obj.get_split_axis_display()

    @admin.display(description="Статус", ordering="status")
    def status_badge(self, obj):
        return _badge(obj.status, obj.get_status_display())

    @admin.display(description="Параметры запроса")
    def params_col(self, obj):
        params = obj.build_params(start=0)
        return format_html(
            '<code style="font-size:12px">{}</code>',
            "&".join(f"{key}={value}" for key, value in params.items()),
        )

    @admin.display(description="Вакансии")
    def vacancies_col(self, obj):
        count = obj.found_vacancies.count()
        if not count:
            return "-"
        return _changelist_link("vacancy", f"{count} шт. →", last_task__id__exact=obj.pk)

    @admin.action(description="↻ Вернуть в очередь (с начала)")
    def action_requeue(self, request, queryset):
        updated = queryset.exclude(status=TaskStatus.RUNNING).update(
            status=TaskStatus.PENDING, pages_done=0, finished_at=None
        )
        self.message_user(
            request,
            f"Возвращено в очередь: {updated}. Будут пройдены заново с первой страницы "
            "при следующем запуске запроса.",
            messages.SUCCESS if updated else messages.INFO,
        )

    @admin.action(description="✔ Пометить пройденной")
    def action_mark_done(self, request, queryset):
        updated = queryset.update(status=TaskStatus.DONE)
        self.message_user(request, f"Помечено пройденными: {updated}.", messages.SUCCESS)


# ------------------------------------------------------------- 5. Запуски

@admin.register(ParseRun)
class ParseRunAdmin(admin.ModelAdmin):
    list_display = (
        "pk",
        "query_col",
        "status_badge",
        "requests_made",
        "jobs_created",
        "jobs_updated",
        "errors_count",
        "duration_col",
        "control_col",
    )
    list_display_links = ("pk", "query_col")
    list_filter = ("status", "search_query")
    search_fields = ("search_query__name", "search_query__keywords", "log", "error_message")
    list_select_related = ("search_query",)
    list_per_page = 50
    actions = ["action_stop", "action_restart"]
    readonly_fields = [
        f.name for f in ParseRun._meta.fields if f.name not in {"id"}
    ] + ["control_col", "vacancies_col"]

    def has_add_permission(self, request):
        return False

    @admin.display(description="Запрос")
    def query_col(self, obj):
        suffix = " · догрузка" if obj.backfill_only else ""
        return f"{obj.search_query.title}{suffix}"

    @admin.display(description="Статус", ordering="status")
    def status_badge(self, obj):
        return _badge(obj.status, obj.get_status_display())

    @admin.display(description="Длительность")
    def duration_col(self, obj):
        delta = obj.duration
        if not delta:
            return "-"
        total = int(delta.total_seconds())
        return f"{total // 60}м {total % 60}с"

    @admin.display(description="Вакансии запуска")
    def vacancies_col(self, obj):
        return _changelist_link("vacancy", "Показать вакансии этого запуска →", last_run__id__exact=obj.pk)

    @admin.display(description="Управление")
    def control_col(self, obj):
        if obj.is_live:
            url = reverse("admin:jobs_parserun_stop", args=[obj.pk])
            return format_html(
                '<a class="button" style="background:#b91c1c;color:#fff;padding:4px 10px;'
                'border-radius:4px;white-space:nowrap" href="{}">⏹ Остановить</a>',
                url,
            )
        url = reverse("admin:jobs_searchquery_run", args=[obj.search_query_id])
        return format_html(
            '<a class="button" style="background:#2563eb;color:#fff;padding:4px 10px;'
            'border-radius:4px;white-space:nowrap" href="{}">▶ Запустить снова</a>',
            url,
        )

    def get_urls(self):
        return [
            path(
                "<int:pk>/stop/",
                self.admin_site.admin_view(self.stop_view),
                name="jobs_parserun_stop",
            ),
        ] + super().get_urls()

    def stop_view(self, request, pk):
        run = get_object_or_404(ParseRun, pk=pk)
        if not self.has_change_permission(request, run):
            self.message_user(request, "Недостаточно прав.", messages.ERROR)
        elif runner.request_stop(run):
            self.message_user(
                request,
                f"Запуск #{run.pk}: запрошена остановка - поток завершится после текущего запроса.",
                messages.SUCCESS,
            )
        else:
            self.message_user(request, f"Запуск #{run.pk} уже не выполняется.", messages.INFO)
        return HttpResponseRedirect(request.META.get("HTTP_REFERER") or reverse("admin:jobs_parserun_changelist"))

    @admin.action(description="⏹ Остановить")
    def action_stop(self, request, queryset):
        stopped = sum(1 for run in queryset if runner.request_stop(run))
        self.message_user(request, f"Остановка запрошена для {stopped} запусков.", messages.SUCCESS if stopped else messages.INFO)

    @admin.action(description="▶ Запустить запрос заново")
    def action_restart(self, request, queryset):
        started, skipped = [], []
        for run in queryset.select_related("search_query"):
            new_run, error = runner.start_run(run.search_query)
            (skipped if error else started).append(error or f"#{new_run.pk}")
        if started:
            self.message_user(request, "Запущено: " + ", ".join(started), messages.SUCCESS)
        for note in skipped:
            self.message_user(request, note, messages.WARNING)
