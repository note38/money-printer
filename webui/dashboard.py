import os
import sys
import time
import json
import shutil
from datetime import datetime
import streamlit as st
from loguru import logger

from app.models import const
from app.utils import utils
from app.services import state as sm
from app.services import task as tm

# Cache task details in memory to avoid repetitive disk I/O on fragment reruns
_TASK_DETAILS_CACHE = {}
_STORAGE_STATS_CACHE = {"timestamp": 0, "size_str": "0 MB"}


def _format_file_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def get_storage_stats() -> str:
    """Calculate the total size of the storage/tasks directory with a 30s cache."""
    now = time.time()
    if now - _STORAGE_STATS_CACHE["timestamp"] < 30:
        return _STORAGE_STATS_CACHE["size_str"]

    tasks_root = utils.task_dir()
    total_size = 0
    if os.path.isdir(tasks_root):
        try:
            for root, _, files in os.walk(tasks_root):
                for f in files:
                    fp = os.path.join(root, f)
                    try:
                        total_size += os.path.getsize(fp)
                    except OSError:
                        pass
        except Exception as e:
            logger.debug(f"Failed to calculate storage size: {e}")

    size_str = _format_file_size(total_size)
    _STORAGE_STATS_CACHE["timestamp"] = now
    _STORAGE_STATS_CACHE["size_str"] = size_str
    return size_str


def invalidate_task_cache(task_path: str = None):
    """Clear memory caches when a task is deleted or modified."""
    if task_path:
        _TASK_DETAILS_CACHE.pop(task_path, None)
    _STORAGE_STATS_CACHE["timestamp"] = 0


def get_task_details(task_path: str) -> dict:
    """Extract complete parameters, script, audio, and materials for a given task."""
    if not os.path.isdir(task_path):
        return {}

    script_path = os.path.join(task_path, "script.json")
    mtime = 0
    if os.path.isfile(script_path):
        try:
            mtime = os.path.getmtime(script_path)
        except OSError:
            pass

    cached = _TASK_DETAILS_CACHE.get(task_path)
    if cached and cached.get("_mtime") == mtime and mtime > 0:
        return cached

    details = {
        "_mtime": mtime,
        "script": "",
        "search_terms": [],
        "params": {},
        "material_sources": [],
        "video_file": None,
        "audio_file": None,
        "subtitle_file": None,
        "video_size_str": "",
    }

    if os.path.isfile(script_path):
        try:
            with open(script_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                details["script"] = data.get("script", "")
                details["search_terms"] = data.get("search_terms", [])
                details["params"] = data.get("params", {})
                details["material_sources"] = data.get("material_sources", [])
        except Exception as e:
            logger.debug(f"Failed to parse script.json for {task_path}: {e}")

    # Check for media files
    audio_p = os.path.join(task_path, "audio.mp3")
    if os.path.isfile(audio_p):
        details["audio_file"] = audio_p

    sub_p = os.path.join(task_path, "subtitle.srt")
    if os.path.isfile(sub_p):
        details["subtitle_file"] = sub_p

    # Video detection (prefer final-1.mp4, then other mp4s)
    final_v = os.path.join(task_path, "final-1.mp4")
    if os.path.isfile(final_v):
        details["video_file"] = final_v
    else:
        for entry in os.listdir(task_path):
            if entry.endswith(".mp4") and not entry.startswith("."):
                details["video_file"] = os.path.join(task_path, entry)
                break

    if details["video_file"] and os.path.isfile(details["video_file"]):
        try:
            details["video_size_str"] = _format_file_size(
                os.path.getsize(details["video_file"])
            )
        except OSError:
            pass

    _TASK_DETAILS_CACHE[task_path] = details
    return details


@st.dialog(title="Task Details", width="large")
def show_task_details_dialog(
    task: dict,
    tr,
    on_restore=None,
    on_delete=None,
    on_open_video=None,
    on_open_path=None,
    on_publish=None,
):
    task_id = task["task_id"]
    task_path = task["task_path"]
    details = get_task_details(task_path)
    params = details.get("params", {})
    video_file = details.get("video_file") or task.get("video_file")
    has_video = bool(video_file and os.path.isfile(video_file))

    # Top subject title & metadata row
    st.subheader(task.get("subject", task_id))
    meta_cols = st.columns([2, 1, 1], vertical_alignment="center")
    meta_cols[0].caption(f"Task ID: `{task_id}`")
    meta_cols[1].caption(
        f"📅 {datetime.fromtimestamp(task.get('mtime', 0)).strftime('%Y-%m-%d %H:%M')}"
    )
    if details.get("video_size_str"):
        meta_cols[2].caption(f"📦 {details['video_size_str']}")

    # Quick action toolbar inside dialog
    btn_cols = st.columns(4)
    with btn_cols[0]:
        if st.button(
            tr("Edit in Studio"),
            key=f"dialog_restore_{task_id}",
            icon=":material/edit:",
            type="primary",
            use_container_width=True,
            help="Load all task parameters into Video Studio",
        ):
            if on_restore:
                on_restore(task_id)
            st.session_state["active_view"] = "studio"
            st.rerun()

    with btn_cols[1]:
        if st.button(
            tr("Open Task Folder"),
            key=f"dialog_folder_{task_id}",
            icon=":material/folder_open:",
            use_container_width=True,
        ):
            if on_open_path:
                on_open_path(task_path)

    with btn_cols[2]:
        can_pub = on_publish and bool(task.get("cross_post_state") != "processing") and has_video
        if st.button(
            tr("Publish Now"),
            key=f"dialog_publish_{task_id}",
            icon=":material/publish:",
            use_container_width=True,
            disabled=not can_pub,
        ):
            if on_publish:
                on_publish(task_id)

    with btn_cols[3]:
        is_busy = task.get("state") == const.TASK_STATE_PROCESSING
        if st.button(
            tr("Delete Task"),
            key=f"dialog_delete_{task_id}",
            icon=":material/delete:",
            use_container_width=True,
            disabled=is_busy,
        ):
            if on_delete:
                if on_delete(task_id, task_path, task.get("state")):
                    invalidate_task_cache(task_path)
                    st.toast(tr("Task Deleted"))
                    st.rerun()

    st.divider()

    # Detail tabs
    tab_labels = [
        f"🎥 {tr('Video') if tr('Video') != 'Video' else 'Video'}",
        f"📝 {tr('Video Script') if tr('Video Script') != 'Video Script' else 'Script'}",
        f"🎵 {tr('Audio Preview')}",
        f"🖼️ {tr('Used Materials')}",
        f"⚙️ {tr('Task Parameters')}",
    ]
    tabs = st.tabs(tab_labels)

    # 1. Video tab
    with tabs[0]:
        if has_video:
            st.video(video_file)
            try:
                with open(video_file, "rb") as vf:
                    st.download_button(
                        label=f"📥 {tr('Download Video')} ({details.get('video_size_str', '')})",
                        data=vf.read(),
                        file_name=f"{task.get('subject', 'video')[:50]}.mp4",
                        mime="video/mp4",
                        key=f"download_btn_{task_id}",
                        use_container_width=True,
                    )
            except Exception as e:
                logger.warning(f"Failed to prepare download for {video_file}: {e}")
        else:
            st.info(tr("No Video Available"))

    # 2. Script tab
    with tabs[1]:
        script_text = details.get("script") or params.get("video_script") or ""
        if script_text:
            st.markdown("##### " + tr("Video Script"))
            st.text_area(
                "Script Content",
                value=script_text,
                height=220,
                disabled=True,
                label_visibility="collapsed",
            )
        else:
            st.info("No script recorded for this task.")

        terms = details.get("search_terms") or params.get("video_terms") or []
        if isinstance(terms, list) and terms:
            st.markdown("##### " + tr("Generate Video Keywords"))
            st.pills("Keywords", terms, selection_mode="single", label_visibility="collapsed")

        # Subtitles
        sub_file = details.get("subtitle_file")
        if sub_file and os.path.isfile(sub_file):
            st.markdown("##### " + tr("Generated Subtitles"))
            try:
                with open(sub_file, "r", encoding="utf-8", errors="ignore") as sf:
                    sub_content = sf.read()
                st.code(sub_content, language="markdown")
            except Exception as e:
                logger.debug(f"Failed to read subtitles: {e}")

    # 3. Audio preview tab
    with tabs[2]:
        audio_file = details.get("audio_file")
        if audio_file and os.path.isfile(audio_file):
            st.audio(audio_file)
            st.caption(f"Audio file: `{os.path.basename(audio_file)}`")
        else:
            st.info(tr("No Audio Available"))

        voice_name = params.get("voice_name")
        if voice_name:
            st.markdown(f"**{tr('Voice')}:** `{voice_name}`")
        if params.get("bgm_file") or params.get("bgm_type"):
            st.markdown(
                f"**BGM:** `{params.get('bgm_file') or params.get('bgm_type')}` (Vol: {params.get('bgm_volume', '-')})"
            )

    # 4. Materials tab
    with tabs[3]:
        sources = details.get("material_sources", [])
        if sources:
            st.caption(f"Found {len(sources)} clip(s) used in this project:")
            for idx, item in enumerate(sources[:20]):
                with st.container(border=True):
                    cols = st.columns([1, 4])
                    cols[0].write(f"#{idx + 1}")
                    creator = item.get("creator", {})
                    creator_name = (
                        creator.get("name") if isinstance(creator, dict) else str(creator)
                    )
                    cols[1].markdown(
                        f"**Provider:** `{item.get('provider', 'unknown')}` | "
                        f"**Duration:** `{item.get('duration', '-')}s` | "
                        f"**Author:** {creator_name or '-'}"
                    )
                    if item.get("source_page"):
                        cols[1].markdown(f"[View Source Asset]({item['source_page']})")
        else:
            st.info(tr("No Materials Recorded"))

    # 5. Parameters tab
    with tabs[4]:
        st.json(params)


def render_kpi_metrics(tasks: list, tr):
    """Render top 4 KPI metric cards."""
    total_count = len(tasks)
    processing_count = sum(
        1 for t in tasks if t.get("state") == const.TASK_STATE_PROCESSING
    )
    completed_count = sum(
        1
        for t in tasks
        if t.get("state") == const.TASK_STATE_COMPLETE
        or (t.get("video_file") and os.path.isfile(t.get("video_file")))
    )
    failed_count = sum(
        1 for t in tasks if t.get("state") == const.TASK_STATE_FAILED
    )
    storage_str = get_storage_stats()

    cols = st.columns(4)
    with cols[0]:
        st.metric(
            label=f"📌 {tr('Total Tasks')}",
            value=total_count,
        )
    with cols[1]:
        st.metric(
            label=f"⚡ {tr('In Progress') if tr('In Progress') != 'In Progress' else 'In Progress'}",
            value=processing_count,
            delta=f"{processing_count} active" if processing_count > 0 else None,
        )
    with cols[2]:
        rate_str = f"{int((completed_count / max(1, total_count)) * 100)}%"
        st.metric(
            label=f"✅ {tr('Task Status Complete')}",
            value=completed_count,
            delta=rate_str if total_count > 0 else None,
        )
    with cols[3]:
        st.metric(
            label=f"💾 {tr('Storage Used')}",
            value=storage_str,
            delta=f"{failed_count} failed" if failed_count > 0 else None,
            delta_color="inverse" if failed_count > 0 else "off",
        )


def _render_task_card(
    task: dict,
    key_prefix: str,
    tr,
    on_restore=None,
    on_delete=None,
    on_open_video=None,
    on_open_path=None,
    on_publish=None,
    can_publish_func=None,
):
    task_id = task["task_id"]
    task_path = task["task_path"]
    details = get_task_details(task_path)
    video_file = details.get("video_file") or task.get("video_file")
    has_video = bool(video_file and os.path.isfile(video_file))
    is_processing = task.get("state") == const.TASK_STATE_PROCESSING
    is_failed = task.get("state") == const.TASK_STATE_FAILED
    progress = int(task.get("progress", 0) or 0)
    params = details.get("params", {})

    with st.container(border=True, key=f"dash_card_{key_prefix}_{task_id[:16]}"):
        # 1. Card Header: Status Tag & Time
        header_cols = st.columns([2, 1], vertical_alignment="center")
        if is_processing:
            header_cols[0].markdown(f"🔵 **{tr('Task Status Processing')}** `{progress}%`")
        elif is_failed:
            header_cols[0].markdown(f"🔴 **{tr('Task Status Failed')}**")
        elif has_video:
            header_cols[0].markdown(f"🟢 **{tr('Task Status Complete')}**")
        else:
            header_cols[0].markdown(f"⚪ **{tr('Task Status History')}**")

        time_str = datetime.fromtimestamp(task.get("mtime", 0)).strftime("%m-%d %H:%M")
        header_cols[1].caption(f"🕒 {time_str}")

        # 2. Media / Preview section
        if has_video:
            st.video(video_file)
        elif is_processing:
            st.progress(progress / 100.0, text=f"{tr('Task Progress')}: {progress}%")
        else:
            st.info(tr("No Video Available"))

        # 3. Card Title & Script Snippet
        st.markdown(f"#### {task.get('subject', 'Untitled Video')}")
        script_snippet = details.get("script") or params.get("video_script") or ""
        if script_snippet:
            shortened = script_snippet[:90] + ("..." if len(script_snippet) > 90 else "")
            st.caption(f"💬 {shortened}")

        # 4. Badges (Aspect, Voice, Size)
        badge_parts = []
        if params.get("video_aspect"):
            badge_parts.append(f"📐 {params['video_aspect']}")
        if params.get("voice_name"):
            short_voice = params["voice_name"].split("-")[-1] if "-" in params["voice_name"] else params["voice_name"]
            badge_parts.append(f"🎙️ {short_voice}")
        if details.get("video_size_str"):
            badge_parts.append(f"📦 {details['video_size_str']}")
        if badge_parts:
            st.caption(" • ".join(badge_parts))

        st.divider()

        # 5. Action Buttons Grid
        action_cols = st.columns(4, gap="small", vertical_alignment="center")
        with action_cols[0]:
            if st.button(
                "🔍",
                key=f"card_det_{key_prefix}_{task_id}",
                use_container_width=True,
                help=tr("Task Details"),
            ):
                show_task_details_dialog(
                    task,
                    tr,
                    on_restore=on_restore,
                    on_delete=on_delete,
                    on_open_video=on_open_video,
                    on_open_path=on_open_path,
                    on_publish=on_publish,
                )

        with action_cols[1]:
            if st.button(
                "🔁",
                key=f"card_rest_{key_prefix}_{task_id}",
                use_container_width=True,
                help=tr("Edit in Studio"),
                disabled=is_processing,
            ):
                if on_restore:
                    on_restore(task_id)
                st.session_state["active_view"] = "studio"
                st.rerun()

        with action_cols[2]:
            if st.button(
                "📂",
                key=f"card_folder_{key_prefix}_{task_id}",
                use_container_width=True,
                help=tr("Open Task Folder"),
            ):
                if on_open_path:
                    on_open_path(task_path)

        with action_cols[3]:
            if st.button(
                "🗑️",
                key=f"card_del_{key_prefix}_{task_id}",
                use_container_width=True,
                help=tr("Delete Task"),
                disabled=is_processing,
            ):
                if on_delete:
                    if on_delete(task_id, task_path, task.get("state")):
                        invalidate_task_cache(task_path)
                        st.toast(tr("Task Deleted"))
                        st.rerun()


def render_dashboard_view(
    tasks: list,
    tr,
    on_restore=None,
    on_delete=None,
    on_open_video=None,
    on_open_path=None,
    on_publish=None,
    can_publish_func=None,
    format_time_func=None,
    format_subject_func=None,
    filter_key_func=None,
):
    """
    Main entry point for the Dashboard View.
    Renders KPIs, search & filter toolbar, and card/table layouts.
    """
    # 1. Top Header Banner with "+ Create New Video" button
    title_col, action_col = st.columns([3.5, 1.2], vertical_alignment="center")
    with title_col:
        st.markdown(
            f"<h2 style='margin:0; padding:0;'>📊 {tr('Dashboard')}</h2>"
            f"<p style='margin:0; color:#888; font-size:0.95rem;'>"
            f"{len(tasks)} {tr('Recent Tasks').lower()} • MoneyPrinterTurbo Video Hub</p>",
            unsafe_allow_html=True,
        )
    with action_col:
        if st.button(
            f"🎬 {tr('Create New Video')}",
            key="dash_create_new_btn",
            type="primary",
            use_container_width=True,
            icon=":material/add:",
        ):
            st.session_state["active_view"] = "studio"
            st.rerun()

    st.write("")

    # 2. KPI Metrics Bar
    render_kpi_metrics(tasks, tr)

    st.divider()

    # 3. Search and Filters Toolbar
    ctrl_cols = st.columns([2.5, 2.0, 1.5], vertical_alignment="center")
    with ctrl_cols[0]:
        search_query = st.text_input(
            "Search",
            placeholder=f"🔍 {tr('Search Tasks')}",
            label_visibility="collapsed",
            key="dash_search_input",
        ).strip().lower()

    with ctrl_cols[1]:
        status_filter = st.pills(
            "Status",
            options=["all", "processing", "complete", "failed"],
            default="all",
            format_func=lambda s: {
                "all": tr("All Tasks"),
                "processing": f"⚡ {tr('Task Status Processing')}",
                "complete": f"✅ {tr('Task Status Complete')}",
                "failed": f"❌ {tr('Task Status Failed')}",
            }.get(s, s),
            label_visibility="collapsed",
            key="dash_status_filter",
        )

    with ctrl_cols[2]:
        view_mode = st.segmented_control(
            "View",
            options=["gallery", "table"],
            default=st.session_state.get("dash_view_mode", "gallery"),
            format_func=lambda m: f"🖼️ {tr('Gallery')}" if m == "gallery" else f"📋 {tr('Table')}",
            label_visibility="collapsed",
            key="dash_view_mode_selector",
        )
        if view_mode:
            st.session_state["dash_view_mode"] = view_mode

    # 4. Filter tasks based on search and status
    filtered = []
    for t in tasks:
        # Status filter
        if status_filter and status_filter != "all":
            if filter_key_func:
                t_key = filter_key_func(t)
            else:
                is_p = t.get("state") == const.TASK_STATE_PROCESSING
                is_c = t.get("state") == const.TASK_STATE_COMPLETE or bool(t.get("video_file"))
                is_f = t.get("state") == const.TASK_STATE_FAILED
                t_key = "processing" if is_p else ("failed" if is_f else ("complete" if is_c else "history"))
            if t_key != status_filter:
                continue

        # Search filter
        if search_query:
            subj = str(t.get("subject", "")).lower()
            tid = str(t.get("task_id", "")).lower()
            script = str(get_task_details(t.get("task_path", "")).get("script", "")).lower()
            if search_query not in subj and search_query not in tid and search_query not in script:
                continue

        filtered.append(t)

    # 5. Content Rendering
    if not filtered:
        st.info(tr("No Tasks Match Filter") if tasks else tr("No Tasks Yet"))
        if not tasks:
            if st.button(f"🎬 {tr('Create New Video')}", key="dash_empty_create_btn"):
                st.session_state["active_view"] = "studio"
                st.rerun()
        return

    st.caption(f"Showing {len(filtered)} / {len(tasks)} task(s)")

    active_mode = st.session_state.get("dash_view_mode", "gallery")
    if active_mode == "gallery":
        # 6-column card grid
        cols_per_row = 6
        for i in range(0, len(filtered), cols_per_row):
            row_items = filtered[i : i + cols_per_row]
            row_cols = st.columns(cols_per_row)
            for col, item in zip(row_cols, row_items):
                with col:
                    _render_task_card(
                        item,
                        key_prefix=f"grid_{i}",
                        tr=tr,
                        on_restore=on_restore,
                        on_delete=on_delete,
                        on_open_video=on_open_video,
                        on_open_path=on_open_path,
                        on_publish=on_publish,
                        can_publish_func=can_publish_func,
                    )
    else:
        # Table view
        with st.container(border=True):
            header_cols = st.columns([1.2, 1.6, 3.2, 1.0, 2.0], vertical_alignment="center")
            header_cols[0].caption(tr("Task Status"))
            header_cols[1].caption(tr("Task Updated At"))
            header_cols[2].caption(tr("Task Subject"))
            header_cols[3].caption(tr("Task Progress"))
            header_cols[4].caption(tr("Task Actions"))

            for idx, task in enumerate(filtered[:50]):
                task_id = task["task_id"]
                task_path = task["task_path"]
                video_file = task.get("video_file")
                has_video = bool(video_file and os.path.isfile(video_file))
                is_processing = task.get("state") == const.TASK_STATE_PROCESSING
                safe_task_key = "".join(ch if ch.isalnum() else "_" for ch in task_id)[:40]

                with st.container(key=f"tbl_row_{idx}_{safe_task_key}", border=True):
                    r_cols = st.columns([1.2, 1.6, 3.2, 1.0, 2.0], vertical_alignment="center")

                    # Status badge
                    if is_processing:
                        r_cols[0].write(f"⚡ {tr('Task Status Processing')}")
                    elif task.get("state") == const.TASK_STATE_FAILED:
                        r_cols[0].write(f"❌ {tr('Task Status Failed')}")
                    elif has_video:
                        r_cols[0].write(f"✅ {tr('Task Status Complete')}")
                    else:
                        r_cols[0].write(f"📁 {tr('Task Status History')}")

                    # Time
                    time_val = task.get("mtime", 0)
                    r_cols[1].write(
                        datetime.fromtimestamp(time_val).strftime("%Y-%m-%d %H:%M")
                        if time_val
                        else "-"
                    )

                    # Subject
                    subj = str(task.get("subject", task_id))
                    r_cols[2].write(subj if len(subj) <= 45 else subj[:45] + "...")

                    # Progress
                    r_cols[3].write(f"{task.get('progress', 0)}%")

                    # Actions
                    a_cols = r_cols[4].columns(4, gap="small", vertical_alignment="center")
                    with a_cols[0]:
                        if st.button(
                            "🔍",
                            key=f"tbl_det_{idx}_{task_id}",
                            help=tr("Task Details"),
                            use_container_width=True,
                        ):
                            show_task_details_dialog(
                                task,
                                tr,
                                on_restore=on_restore,
                                on_delete=on_delete,
                                on_open_video=on_open_video,
                                on_open_path=on_open_path,
                                on_publish=on_publish,
                            )
                    with a_cols[1]:
                        if st.button(
                            "🔁",
                            key=f"tbl_rest_{idx}_{task_id}",
                            help=tr("Edit in Studio"),
                            use_container_width=True,
                            disabled=is_processing,
                        ):
                            if on_restore:
                                on_restore(task_id)
                            st.session_state["active_view"] = "studio"
                            st.rerun()
                    with a_cols[2]:
                        if st.button(
                            "📂",
                            key=f"tbl_open_{idx}_{task_id}",
                            help=tr("Open Task Folder"),
                            use_container_width=True,
                        ):
                            if on_open_path:
                                on_open_path(task_path)
                    with a_cols[3]:
                        if st.button(
                            "🗑️",
                            key=f"tbl_del_{idx}_{task_id}",
                            help=tr("Delete Task"),
                            use_container_width=True,
                            disabled=is_processing,
                        ):
                            if on_delete:
                                if on_delete(task_id, task_path, task.get("state")):
                                    invalidate_task_cache(task_path)
                                    st.toast(tr("Task Deleted"))
                                    st.rerun()
