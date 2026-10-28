"""Идемпотентно создаёт суперпользователя из переменных окружения."""

import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Создаёт или обновляет суперпользователя по DJANGO_SUPERUSER_* из .env"

    def handle(self, *args, **options):
        username = os.environ.get("DJANGO_SUPERUSER_USERNAME", "admin").strip()
        password = os.environ.get("DJANGO_SUPERUSER_PASSWORD", "admin")
        email = os.environ.get("DJANGO_SUPERUSER_EMAIL", "").strip()

        if not username or not password:
            self.stdout.write(self.style.WARNING("DJANGO_SUPERUSER_USERNAME/PASSWORD пустые - пропускаю."))
            return

        User = get_user_model()
        user, created = User.objects.get_or_create(
            username=username,
            defaults={"email": email, "is_staff": True, "is_superuser": True},
        )
        user.is_staff = True
        user.is_superuser = True
        if email:
            user.email = email
        if created or os.environ.get("DJANGO_SUPERUSER_FORCE_PASSWORD", "").lower() in {"1", "true", "yes"}:
            user.set_password(password)
        user.save()

        if created:
            self.stdout.write(self.style.SUCCESS(f"Суперпользователь создан: {username}"))
        else:
            self.stdout.write(f"Суперпользователь уже есть: {username} (пароль не трогаю)")
