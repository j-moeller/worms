from core.models.competition import ApplicationSetting, Competition, Round
from core.models.logging import Logging, LoggingAttempt
from core.models.scoring import FlagCapture, FlagCaptureAttempt
from core.models.team import AccessTokens, Team, TeamMembership
from core.models.user import CompetitionParticipant, User
from core.models.vm import VirtualMachine
from core.models.worms import MalwareArtifact, Upload, WormDeploymentPlan
from django.contrib import admin


@admin.register(Competition)
class CompetitionAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "registration_starts_at",
        "registration_ends_at",
        "registration_is_open_display",
        "created_at",
    )
    list_filter = ("created_at",)
    search_fields = ("name",)
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    readonly_fields = ("created_at",)

    @admin.display(boolean=True, description="Registration open")
    def registration_is_open_display(self, obj):
        return obj.registration_is_open


@admin.register(ApplicationSetting)
class ApplicationSettingAdmin(admin.ModelAdmin):
    list_display = ("key", "current_competition")
    list_filter = ("current_competition",)
    search_fields = (
        "key",
        "current_competition__name",
    )
    autocomplete_fields = ("current_competition",)

    def has_add_permission(self, request):
        # This table is intended to contain exactly one row.
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
    list_filter = (
        "competition",
        "round_start",
        "round_end",
    )
    search_fields = (
        "name",
        "competition__name",
    )
    autocomplete_fields = ("competition",)
    date_hierarchy = "created_at"
    ordering = ("competition", "round_start")
    readonly_fields = ("created_at",)


@admin.register(LoggingAttempt)
class LoggingAttemptAdmin(admin.ModelAdmin):
    list_display = (
        "uuid",
        "processed",
        "processed_at",
        "created_at",
    )
    list_filter = ("processed", "created_at")
    search_fields = ("uuid",)
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    readonly_fields = (
        "uuid",
        "team_token_hash",
        "log_data",
        "processed_at",
        "created_at",
    )

    @admin.display(description="Log data")
    def log_data_display(self, obj):
        return obj.log_data


@admin.register(Logging)
class LoggingAdmin(admin.ModelAdmin):
    list_display = (
        "team",
        "round",
        "logging_attempt",
        "created_at",
    )
    list_filter = (
        "round__competition",
        "round",
        "created_at",
    )
    search_fields = (
        "team__name",
        "team__uuid",
        "round__name",
        "logging_attempt__uuid",
    )
    autocomplete_fields = (
        "team",
        "round",
        "logging_attempt",
    )
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    readonly_fields = ("created_at",)


@admin.register(FlagCaptureAttempt)
class FlagCaptureAttemptAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "processed",
        "processed_at",
        "created_at",
    )
    list_filter = ("processed", "created_at")
    search_fields = ("id",)
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    readonly_fields = (
        "team_token_hash",
        "vm_flag_hash",
        "processed_at",
        "created_at",
    )


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
        "round__competition",
        "round",
        "created_at",
    )
    search_fields = (
        "team__name",
        "team__uuid",
        "vm__vm_identifier",
        "flag_capture_attempt__id",
    )
    autocomplete_fields = (
        "team",
        "round",
        "vm",
        "flag_capture_attempt",
    )
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    readonly_fields = ("created_at",)


@admin.register(AccessTokens)
class AccessTokensAdmin(admin.ModelAdmin):
    list_display = (
        "team",
        "revoked",
        "revoked_at",
        "created_at",
    )
    list_filter = (
        "revoked",
        "created_at",
    )
    search_fields = (
        "team__name",
        "team__uuid",
    )
    autocomplete_fields = ("team",)
    date_hierarchy = "created_at"
    ordering = ("-created_at",)

    # Never display the actual token hash in the admin list.
    readonly_fields = (
        "access_token_hash",
        "created_at",
    )


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = (
        "username",
        "email",
        "first_name",
        "last_name",
        "is_staff",
        "is_superuser",
        "is_active",
        "created_at",
    )
    list_filter = (
        "is_staff",
        "is_superuser",
        "is_active",
        "groups",
        "created_at",
    )
    search_fields = (
        "username",
        "email",
        "first_name",
        "last_name",
        "uuid",
    )
    date_hierarchy = "created_at"
    ordering = ("username",)
    readonly_fields = (
        "uuid",
        "created_at",
        "last_login",
        "date_joined",
    )


@admin.register(CompetitionParticipant)
class CompetitionParticipantAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "competition",
        "joined_at",
    )
    list_filter = (
        "competition",
        "joined_at",
    )
    search_fields = (
        "user__username",
        "user__email",
        "user__uuid",
        "competition__name",
    )
    autocomplete_fields = (
        "user",
        "competition",
    )
    date_hierarchy = "joined_at"
    ordering = ("-joined_at",)
    readonly_fields = ("joined_at",)


@admin.register(VirtualMachine)
class VirtualMachineAdmin(admin.ModelAdmin):
    list_display = (
        "vm_identifier",
        "round",
        "vm_maintenance_ip",
        "created_at",
    )
    list_filter = (
        "round__competition",
        "round",
        "created_at",
    )
    search_fields = (
        "vm_identifier",
        "vm_maintenance_ip",
        "round__name",
        "round__competition__name",
    )
    autocomplete_fields = ("round",)
    date_hierarchy = "created_at"
    ordering = ("round", "vm_identifier")
    readonly_fields = (
        "ctf_flag_hash",
        "created_at",
    )


@admin.register(MalwareArtifact)
class MalwareArtifactAdmin(admin.ModelAdmin):
    list_display = (
        "content_hash",
        "storage_key",
        "upload_count",
        "created_at",
    )
    search_fields = (
        "content_hash",
        "storage_key",
    )
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    readonly_fields = (
        "content_hash",
        "created_at",
    )

    @admin.display(description="Uploads")
    def upload_count(self, obj):
        return obj.uploads.count()


@admin.register(Upload)
class UploadAdmin(admin.ModelAdmin):
    list_display = (
        "artifact",
        "user",
        "created_at",
    )
    list_filter = ("created_at",)
    search_fields = (
        "artifact__content_hash",
        "artifact__storage_key",
        "user__username",
        "user__email",
    )
    autocomplete_fields = (
        "artifact",
        "user",
    )
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    readonly_fields = ("created_at",)


@admin.register(WormDeploymentPlan)
class WormDeploymentPlanAdmin(admin.ModelAdmin):
    list_display = (
        "team",
        "virtual_machine",
        "upload",
        "created_at",
    )
    list_filter = (
        "virtual_machine__round__competition",
        "virtual_machine__round",
        "virtual_machine",
        "created_at",
    )
    search_fields = (
        "team__name",
        "team__uuid",
        "virtual_machine__vm_identifier",
        "virtual_machine__round__name",
        "upload__artifact__content_hash",
        "upload__user__username",
    )
    autocomplete_fields = (
        "virtual_machine",
        "team",
        "upload",
    )
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    readonly_fields = ("created_at",)


class TeamMembershipInline(admin.TabularInline):
    model = TeamMembership
    extra = 1
    autocomplete_fields = ("user",)
    readonly_fields = ("created_at",)


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "competition",
        "max_members",
        "member_count",
        "uuid",
        "created_at",
    )
    list_filter = (
        "competition",
        "created_at",
    )
    search_fields = (
        "name",
        "uuid",
        "competition__name",
        "users__username",
        "users__email",
    )
    autocomplete_fields = ("competition",)
    inlines = (TeamMembershipInline,)
    date_hierarchy = "created_at"
    ordering = ("competition", "name")
    readonly_fields = ("uuid", "created_at")

    @admin.display(description="Members")
    def member_count(self, obj):
        return obj.users.count()


@admin.register(TeamMembership)
class TeamMembershipAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "team",
        "created_at",
    )
    list_filter = (
        "team__competition",
        "created_at",
    )
    search_fields = (
        "user__username",
        "user__email",
        "team__name",
        "team__uuid",
    )
    autocomplete_fields = (
        "user",
        "team",
    )
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    readonly_fields = ("created_at",)
