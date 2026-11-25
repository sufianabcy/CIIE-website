import csv
from io import BytesIO, StringIO

from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.db import DatabaseError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from openpyxl import Workbook

from . import engine
from .models import SqlRun

SESSION_KEY = "sqlconsole_dry_run"


@admin.register(SqlRun)
class SqlConsoleAdmin(admin.ModelAdmin):
    """The SQL section of the admin: the console itself, with the run log under it."""

    def has_module_permission(self, request):
        return request.user.is_active and request.user.is_portal_admin

    def has_view_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        if not self.has_module_permission(request):
            raise PermissionDenied
        if request.GET.get("download") and request.GET.get("run"):
            return self._download(request)
        user = request.user
        ctx = {"sql": "", "mode": "read", "limit": engine.ROW_LIMIT}
        if request.method == "POST":
            sql = request.POST.get("sql", "")
            action = request.POST.get("action", "read")
            ctx.update(sql=sql, mode="write" if action in ("dry_run", "commit") else "read")
            try:
                limit = max(1, min(int(request.POST.get("limit") or engine.ROW_LIMIT), 5000))
            except ValueError:
                limit = engine.ROW_LIMIT
            ctx["limit"] = limit
            if action == "read":
                ctx.update(self._read(user, sql, limit))
            elif action in ("dry_run", "commit"):
                if not user.is_superuser:
                    raise PermissionDenied  # writes are for superusers only
                ctx.update(self._write(request, sql, commit=action == "commit"))
        ctx.update(
            self.admin_site.each_context(request),
            title="SQL console",
            can_write=user.is_superuser,
            history=SqlRun.objects.select_related("user")[:30],
            opts=self.model._meta,
            dry=request.session.get(SESSION_KEY),
        )
        return TemplateResponse(request, "sqlconsole/console.html", ctx)

    def _read(self, user, sql, limit):
        run = SqlRun(user=user, query=sql, mode=SqlRun.Mode.READ)
        try:
            columns, rows, truncated, ms = engine.run_read(sql, limit)
        except (engine.Refused, DatabaseError) as exc:
            run.error = str(exc).strip()
            run.save()
            return {"error": run.error}
        run.rows, run.duration_ms = len(rows), ms
        run.save()
        return {"columns": columns, "rows": rows, "truncated": truncated, "run": run}

    def _write(self, request, sql, commit):
        user = request.user
        run = SqlRun(user=user, query=sql, mode=SqlRun.Mode.WRITE if commit else SqlRun.Mode.DRY_RUN)
        expected = None
        if commit:
            dry = request.session.get(SESSION_KEY) or {}
            confirm = request.POST.get("confirm", "").strip()
            if dry.get("sql") != sql.strip():
                run.error = "Run the dry run for this exact statement first."
            elif confirm not in (str(dry.get("rows")), "CONFIRM"):
                run.error = f"Type {dry.get('rows')} (the number of rows) or CONFIRM to commit."
            if run.error:
                run.save()
                return {"error": run.error}
            expected = dry["rows"]
        try:
            rows, columns, preview, ms = engine.run_write(sql, user, commit=commit, expected_rows=expected)
        except (engine.Refused, DatabaseError) as exc:
            run.error = str(exc).strip()
            run.save()
            request.session.pop(SESSION_KEY, None)
            return {"error": run.error}
        run.rows, run.duration_ms, run.committed = rows, ms, commit
        run.save()
        if commit:
            request.session.pop(SESSION_KEY, None)
            messages.success(request, f"Committed: {rows} row(s) changed. The change history records you as the editor.")
            return {"sql": ""}
        request.session[SESSION_KEY] = {"sql": sql.strip(), "rows": rows}
        return {"dry_rows": rows, "columns": columns, "rows": preview, "run": run}

    def _download(self, request):
        run = get_object_or_404(SqlRun, pk=request.GET["run"], mode=SqlRun.Mode.READ)
        try:
            columns, rows, _, _ = engine.run_read(run.query, 5000)
        except (engine.Refused, DatabaseError) as exc:
            messages.error(request, str(exc))
            return redirect("admin:sqlconsole_sqlrun_changelist")
        if request.GET["download"] == "csv":
            out = StringIO()
            writer = csv.writer(out)
            writer.writerow(columns)
            writer.writerows(rows)
            response = HttpResponse(out.getvalue(), content_type="text/csv")
            response["Content-Disposition"] = f'attachment; filename="query-{run.pk}.csv"'
            return response
        wb = Workbook()
        ws = wb.active
        ws.append(columns)
        for row in rows:
            ws.append([v if isinstance(v, (int, float, str)) or v is None else str(v) for v in row])
        buffer = BytesIO()
        wb.save(buffer)
        response = HttpResponse(buffer.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        response["Content-Disposition"] = f'attachment; filename="query-{run.pk}.xlsx"'
        return response
