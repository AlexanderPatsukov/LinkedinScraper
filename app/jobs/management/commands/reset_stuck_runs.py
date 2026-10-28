"""Снимает «подвисшие» запуски после перезапуска контейнера.

Поток парсера живёт внутри процесса Django. Если контейнер перезапустили,
поток умер, а запись в БД осталась в статусе «Выполняется» - эта команда
переводит такие запуски в «Ошибка», чтобы запрос можно было запустить снова.
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from jobs.models import ParseRun, RunStatus


class Command(BaseCommand):
    help = "Помечает незавершённые запуски как прерванные"

    def handle(self, *args, **options):
        stuck = ParseRun.objects.filter(
            status__in=[RunStatus.PENDING, RunStatus.RUNNING, RunStatus.STOPPING]
        )
        count = 0
        for run in stuck:
            run.status = RunStatus.FAILED
            run.error_message = "Прервано перезапуском контейнера. Запустите запрос заново."
            run.finished_at = timezone.now()
            run.append_log("Прервано перезапуском контейнера.", save=False)
            run.save()
            count += 1

        if count:
            self.stdout.write(self.style.WARNING(f"Прерванных запусков снято: {count}"))
        else:
            self.stdout.write("Подвисших запусков нет.")
