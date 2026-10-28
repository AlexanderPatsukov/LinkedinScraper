"""Запуск поискового запроса из консоли - удобно для cron и отладки.

    docker compose exec web python manage.py run_search --id 1
    docker compose exec web python manage.py run_search --all-active
"""

from django.core.management.base import BaseCommand, CommandError

from jobs import runner
from jobs.models import ParseRun, SearchQuery


class Command(BaseCommand):
    help = "Выполняет парсинг по поисковому запросу (синхронно, в текущем процессе)"

    def add_arguments(self, parser):
        parser.add_argument("--id", type=int, help="ID поискового запроса")
        parser.add_argument("--all-active", action="store_true", help="Все активные запросы по очереди")
        parser.add_argument("--list", action="store_true", help="Показать запросы и выйти")
        parser.add_argument(
            "--backfill",
            action="store_true",
            help="Не обходить список: добрать недостающие описания и профили компаний",
        )

    def handle(self, *args, **options):
        if options["list"]:
            for query in SearchQuery.objects.all():
                flag = "✓" if query.is_active else " "
                self.stdout.write(f"[{flag}] #{query.pk}  {query.title}  (макс. запросов: {query.max_requests})")
            return

        if options["all_active"]:
            queries = list(SearchQuery.objects.filter(is_active=True))
        elif options["id"]:
            queries = list(SearchQuery.objects.filter(pk=options["id"]))
            if not queries:
                raise CommandError(f"Поисковый запрос #{options['id']} не найден")
        else:
            raise CommandError("Укажите --id, --all-active или --list")

        for query in queries:
            mode = " (догрузка)" if options["backfill"] else ""
            self.stdout.write(f"=== {query.title}{mode} ===")
            run = ParseRun.objects.create(search_query=query, backfill_only=options["backfill"])
            run.append_log(f"Запуск из консоли{mode}.")
            runner.execute_run(run.pk)
            run.refresh_from_db()
            self.stdout.write(
                self.style.SUCCESS(
                    f"#{run.pk} {run.get_status_display()}: страниц {run.requests_made}, "
                    f"новых {run.jobs_created}, обновлено {run.jobs_updated}, ошибок {run.errors_count}"
                )
            )
            if run.error_message:
                self.stdout.write(self.style.ERROR(run.error_message))
