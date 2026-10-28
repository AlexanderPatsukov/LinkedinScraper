"""Выгрузка вакансий вместе с данными компаний в CSV и Excel."""

from __future__ import annotations

import csv
import io
from datetime import datetime

from django.http import HttpResponse
from django.utils import timezone

COLUMNS: list[tuple[str, str]] = [
    ("job_id", "LinkedIn job ID"),
    ("title", "Вакансия"),
    ("company_display", "Компания"),
    ("location", "Локация"),
    ("posted_raw", "Размещена (текст)"),
    ("posted_at", "Дата размещения"),
    ("is_reposted", "Повторно"),
    ("applicants_raw", "Откликов"),
    ("employment_type", "Тип занятости"),
    ("seniority", "Уровень"),
    # "Функция" (job function) на гостевых страницах LinkedIn не отдаётся вообще -
    # в выгрузке была бы всегда пустой колонкой. Поле в модели осталось: если
    # LinkedIn когда-нибудь начнёт его отдавать, оно заполнится и будет видно в админке.
    ("industries", "Отрасли вакансии"),
    ("benefits", "Плюшки"),
    ("status_display", "Статус обработки"),
    ("detail_error", "Ошибка обработки"),
    ("url", "Ссылка на вакансию"),
    ("company_url", "Ссылка на компанию"),
    ("company_industry", "Отрасль компании"),
    ("company_size", "Размер компании"),
    ("company_headquarters", "Штаб-квартира"),
    ("company_website", "Сайт компании"),
    ("company_followers", "Подписчиков"),
    ("company_tagline", "Компания: короткое описание"),
    ("company_description", "Компания: описание"),
    ("queries", "Найдена по запросам"),
    ("times_seen", "Раз встречена"),
    ("created_at", "Добавлена"),
    ("description", "Описание вакансии"),
]


def _naive(value):
    """Excel не умеет timezone-aware datetime."""
    if isinstance(value, datetime) and timezone.is_aware(value):
        return timezone.localtime(value).replace(tzinfo=None)
    return value


def row_for(vacancy) -> dict:
    company = vacancy.company
    return {
        "job_id": vacancy.job_id,
        "title": vacancy.title,
        "company_display": (company.name if company else "") or vacancy.company_name_raw,
        "location": vacancy.location,
        "posted_raw": vacancy.posted_raw,
        "posted_at": vacancy.posted_at,
        "is_reposted": "да" if vacancy.is_reposted else "нет",
        "applicants_raw": vacancy.applicants_raw,
        "employment_type": vacancy.employment_type,
        "seniority": vacancy.seniority,
        "job_function": vacancy.job_function,
        "industries": vacancy.industries,
        "benefits": vacancy.benefits,
        "status_display": vacancy.get_status_display(),
        "detail_error": vacancy.detail_error,
        "url": vacancy.url,
        "company_url": (company.linkedin_url if company else ""),
        "company_industry": (company.industry if company else ""),
        "company_size": (company.company_size if company else ""),
        "company_headquarters": (company.headquarters if company else ""),
        "company_website": (company.website if company else ""),
        "company_followers": (company.followers if company else ""),
        "company_tagline": (company.tagline if company else ""),
        "company_description": (company.description if company else ""),
        "queries": ", ".join(q.title for q in vacancy.search_queries.all()),
        "times_seen": vacancy.times_seen,
        "created_at": _naive(vacancy.created_at),
        "description": vacancy.description,
    }


def _filename(extension: str) -> str:
    return f"vacancies_{timezone.localtime().strftime('%Y%m%d_%H%M')}.{extension}"


def queryset_for_export(queryset):
    return queryset.select_related("company").prefetch_related("search_queries")


def export_csv(queryset) -> HttpResponse:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    writer.writerow([label for _key, label in COLUMNS])

    for vacancy in queryset_for_export(queryset).iterator(chunk_size=200):
        row = row_for(vacancy)
        writer.writerow(["" if row[key] is None else str(row[key]) for key, _label in COLUMNS])

    # utf-8-sig - чтобы Excel не ломал кириллицу при двойном клике
    response = HttpResponse(buffer.getvalue().encode("utf-8-sig"), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{_filename("csv")}"'
    return response


def export_xlsx(queryset) -> HttpResponse:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font
    from openpyxl.utils import get_column_letter

    workbook = Workbook(write_only=False)
    sheet = workbook.active
    sheet.title = "Вакансии"

    sheet.append([label for _key, label in COLUMNS])
    header_font = Font(bold=True)
    for cell in sheet[1]:
        cell.font = header_font
        cell.alignment = Alignment(vertical="center")
    sheet.freeze_panes = "A2"

    for vacancy in queryset_for_export(queryset).iterator(chunk_size=200):
        row = row_for(vacancy)
        values = []
        for key, _label in COLUMNS:
            value = row[key]
            if value is None:
                value = ""
            elif not isinstance(value, (int, float, datetime)) and not isinstance(value, str):
                value = str(value)
            if isinstance(value, str):
                # в ячейку Excel влезает 32767 символов
                value = value[:32000]
            values.append(value)
        sheet.append(values)

    widths = {
        "Вакансия": 45,
        "Компания": 28,
        "Локация": 28,
        "Ссылка на вакансию": 50,
        "Ссылка на компанию": 40,
        "Компания: описание": 60,
        "Описание вакансии": 80,
    }
    for index, (_key, label) in enumerate(COLUMNS, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = widths.get(label, 20)

    buffer = io.BytesIO()
    workbook.save(buffer)
    response = HttpResponse(
        buffer.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="{_filename("xlsx")}"'
    return response
