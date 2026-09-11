from django.contrib import admin

from .models import (
    ApplicationSetting,
    Competition,
    FlagCapture,
    FlagCaptureAttempt,
    Logging,
    LoggingAttempt,
    MalwareArtifact,
    Round,
    Student,
    Team,
    Upload,
    User,
    VirtualMachine,
    WormDeployment,
)


@admin.register(Competition)
class CompetitionAdmin(admin.ModelAdmin):
    list_display = ("name", "created_at")
    search_fields = ("name",)
    ordering = ("-created_at",)


@admin.register(ApplicationSetting)
class ApplicationSettingAdmin(admin.ModelAdmin):
    list_display = ("key", "current_competition")
    list_filter = ("current_competition",)
    search_fields = ("key",)

    def has_add_permission(self, request):
        # There should only ever be one "current" setting.
        return not ApplicationSetting.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Round)
class RoundAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "competition",
        "seed",
        "round_start",
        "round_end",
        "created_at",
    )
    list_filter = ("competition",)
    search_fields = ("name",)
    ordering = ("-round_start",)


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "competition",
        "created_at",
    )
    list_filter = ("competition",)
    search_fields = ("name",)
    ordering = ("competition", "name")

    # Never expose the token hash unnecessarily in the admin UI.
    exclude = ("access_token_hash",)


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = (
        "username",
        "first_name",
        "last_name",
        "email",
        "team",
        "is_active",
        "is_staff",
        "created_at",
    )
    list_filter = (
        "is_active",
        "is_staff",
        "is_superuser",
        "team",
    )
    search_fields = (
        "username",
        "email",
        "first_name",
        "last_name",
    )
    ordering = ("username",)

    fieldsets = (
        (
            None,
            {
                "fields": (
                    "username",
                    "password",
                )
            },
        ),
        (
            "Personal information",
            {
                "fields": (
                    "first_name",
                    "last_name",
                    "email",
                )
            },
        ),
        (
            "Application",
            {"fields": ("team",)},
        ),
        (
            "Permissions",
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                )
            },
        ),
        (
            "Important dates",
            {
                "fields": (
                    "last_login",
                    "date_joined",
                    "created_at",
                )
            },
        ),
    )
    readonly_fields = (
        "last_login",
        "date_joined",
        "created_at",
    )


@admin.register(Student)
class StudentAdmin(admin.ModelAdmin):
    list_display = (
        "matriculation_number",
        "user",
        "created_at",
    )
    search_fields = (
        "matriculation_number",
        "user__first_name",
        "user__last_name",
        "user__username",
    )
    ordering = ("user__last_name", "user__first_name")


@admin.register(VirtualMachine)
class VirtualMachineAdmin(admin.ModelAdmin):
    list_display = (
        "vm_identifier",
        "round",
        "vm_maintenance_ip",
        "created_at",
    )
    list_filter = ("round",)
    search_fields = (
        "vm_identifier",
        "vm_maintenance_ip",
    )
    ordering = ("round", "vm_identifier")

    exclude = ("ctf_flag_hash",)


@admin.register(FlagCaptureAttempt)
class FlagCaptureAttemptAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "team_token_hash",
        "vm_flag_hash",
        "processed",
        "processed_at",
        "created_at",
    )
    list_filter = ("processed",)
    search_fields = (
        "team_token_hash",
        "vm_flag_hash",
    )
    ordering = ("-created_at",)
    readonly_fields = ("created_at",)


@admin.register(FlagCapture)
class FlagCaptureAdmin(admin.ModelAdmin):
    list_display = (
        "team",
        "round",
        "vm",
        "flag_capture_attempt",
        "created_at",
    )
    list_filter = (
        "round",
        "team",
    )
    search_fields = (
        "team__name",
        "vm__vm_identifier",
    )
    ordering = ("-created_at",)
    readonly_fields = ("created_at",)


@admin.register(LoggingAttempt)
class LoggingAttemptAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "team_token_hash",
        "processed",
        "processed_at",
        "created_at",
    )
    list_filter = ("processed",)
    search_fields = ("team_token_hash",)
    ordering = ("-created_at",)
    readonly_fields = ("created_at",)


@admin.register(Logging)
class LoggingAdmin(admin.ModelAdmin):
    list_display = (
        "team",
        "logging_attempt",
        "created_at",
    )
    list_filter = ("team",)
    search_fields = ("team__name",)
    ordering = ("-created_at",)
    readonly_fields = ("created_at",)


@admin.register(MalwareArtifact)
class MalwareArtifactAdmin(admin.ModelAdmin):
    list_display = (
        "content_hash",
        "storage_key",
        "created_at",
    )
    search_fields = (
        "content_hash",
        "storage_key",
    )
    ordering = ("-created_at",)
    readonly_fields = ("created_at",)


@admin.register(Upload)
class UploadAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "artifact",
        "created_at",
    )
    list_filter = ("user",)
    search_fields = (
        "user__username",
        "artifact__content_hash",
    )
    ordering = ("-created_at",)
    readonly_fields = ("created_at",)


@admin.register(WormDeployment)
class WormDeploymentAdmin(admin.ModelAdmin):
    list_display = (
        "round",
        "team",
        "upload",
        "created_at",
    )
    list_filter = (
        "round",
        "team",
    )
    search_fields = (
        "team__name",
        "upload__artifact__content_hash",
    )
    ordering = ("-created_at",)
    readonly_fields = ("created_at",)
