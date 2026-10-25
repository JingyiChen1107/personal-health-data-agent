"""Minimal Day 3 chat interface; real model configuration is required."""
from uuid import uuid4
import streamlit as st
from health_agent import app_support
from health_agent.config import DEFAULT_DATABASE, load_config
from health_agent.model_config import ModelConfigError
st.set_page_config(page_title='Health Data Agent', page_icon='📋', layout='centered')
cfg = load_config()
from health_agent.i18n import ui_text, indicator_label
ui_choice = st.sidebar.radio('语言 / Language', ['中文', 'English'], key='ui_language', horizontal=True)
LANG = 'en' if ui_choice == 'English' else 'zh'

def t(text):
    return ui_text(text, LANG)
st.title(t('个人健康数据分析 Agent'))
st.caption('支持中英文提问；回答跟随问题语言，原文引用保持原样。' if LANG=='zh' else 'Ask in Chinese or English; replies follow the question language. Source quotations remain unchanged.')
st.caption(t(f'模拟数据 · 参考日期 {cfg.reference_date.isoformat()} · {cfg.timezone}'))
st.caption(t('当前支持指标查询、用药记录和前后比较。支持同一会话追问恢复与条件承接；已支持本地报告检索与原文引用；已支持趋势图；图表使用查询得到的原始记录。'))
if 'session_id' not in st.session_state:
    st.session_state.session_id = uuid4().hex
    st.session_state.chat_history = []
    st.session_state.pending_payload = None
readiness = app_support.configuration_status()
database_ready = DEFAULT_DATABASE.is_file()
with st.sidebar:
    st.subheader(t('运行状态'))
    st.caption(t('会话：') + st.session_state.session_id[:8])
    st.caption(t('当前会话支持承接问题；刷新页面可能开始新会话。'))
    st.write(t('数据：') + (t('已准备') if database_ready else t('需要初始化')))
    st.write(t('模型：') + (t('已配置') if readiness['configured'] else t('待配置')))
    if readiness['configured']:
        st.caption(readiness['model'])
        if readiness.get('provider') == 'ollama':
            st.caption(t('本机 Ollama 推理 · 无需 API 密钥'))
    if st.button(t('新建会话'), key='new_session'):
        st.session_state.session_id = uuid4().hex
        st.session_state.chat_history = []
        st.session_state.pending_payload = None
        st.rerun()
    st.subheader(t('报告索引'))
    from health_agent.db import connect
    from health_agent.rag import upload_report, build_index
    if database_ready:
        with connect(DEFAULT_DATABASE, readonly=True) as con:
            reports = [dict(row) for row in con.execute('SELECT report_id,report_date,index_status FROM reports WHERE patient_id=? ORDER BY report_date', (cfg.patient_id,))]
        for report in reports:
            st.caption(f"{report['report_id']} · {report['report_date']} · {report['index_status']}")
        with st.expander(t('上传虚构TXT报告')):
            st.caption(t('UTF-8 TXT · 每份最多1MB · 总计最多5份 · 日期按报告日期筛选'))
            uploaded = st.file_uploader(t('选择TXT'), type=['txt'], key='report_upload', max_upload_size=1)
            report_day = st.date_input(t('报告日期'), value=cfg.reference_date, max_value=cfg.reference_date)
            synthetic = st.checkbox(t('确认文件仅含虚构演示数据'))
            if st.button(t('上传并建立索引'), disabled=uploaded is None or not synthetic):
                try:
                    with st.spinner(t('正在本机解析和向量化…')):
                        uploaded_result = upload_report(DEFAULT_DATABASE, patient_id=cfg.patient_id, file_name=uploaded.name, content=uploaded.getvalue(), report_date=report_day.isoformat(), synthetic_confirmed=synthetic)
                    if uploaded_result['status'] == 'ready':
                        st.success((t('内容重复，使用已有报告：') if uploaded_result['duplicate'] else t('已可检索：')) + uploaded_result['report_id'])
                    else:
                        st.error(t('报告已登记，但索引失败；请检查本地embedding服务后重建索引。'))
                except (ValueError, OSError):
                    st.error(t('上传失败：请检查UTF-8编码、文件大小、报告日期和总份数。'))
        if st.button(t('重建报告索引')):
            with st.spinner(t('在本机重建报告索引…')):
                try:
                    rebuilt = build_index(DEFAULT_DATABASE)
                except Exception:
                    rebuilt = {'ok': False}
            if rebuilt['ok']:
                st.success(t('报告索引重建完成；刷新可更新状态。'))
            else:
                st.error(t('部分报告索引失败，尚未可检索。'))
    st.subheader(t('示例问题'))
    st.write(t('我最近一次血糖是多少？'))
    st.write(t('画出2026年10月1日至23日的体重趋势。'))
    st.write(t('去年的体检报告有没有提到LDL？'))
    st.write(t('R003报告对血糖记录整理是怎么说的？'))
    st.write(t('我当前有哪些用药记录？'))
    st.write(t('比较2026年10月1日二甲双胍调整前后14天的空腹血糖。'))
if not database_ready:
    st.error(t('数据库尚未初始化，请先运行 python -m health_agent init。'))
if not readiness['configured']:
    st.info('Model configuration is not ready. Keep credentials in local settings and reload after configuration.' if LANG == 'en' else readiness['message'] + t(' 密钥只保存在本地，不要发送到聊天中。配置完成后刷新页面。'))

def render_answer(result):
    if result.get('answer_origin') == 'record_receipt':
        st.caption(t('记录查询回执 · 明细直接来自工具，未由模型改写'))
    if result.get('answer_origin') == 'business_receipt':
        st.caption(t('工具业务回执 · ') + {'no_data': t('无数据'), 'unsupported': t('不支持'), 'insufficient_data': t('样本或窗口不足'), 'conflicting_events': t('窗口内有其他变更')}.get(result['business_status'], result['business_status']))
    if result.get('answer_origin') == 'tool_receipt':
        st.caption(t('图表工具回执 · 日期、点数与单位来自查询快照'))
    st.markdown(result['answer'])
    if result['status'] == 'error':
        st.caption(t('本轮未完成，可查看已执行的查询。'))
    from pathlib import Path
    from health_agent.plotting import PLOT_ROOT
    for tool_result in result.get('tool_results', []):
        chart = tool_result.get('data', {}).get('chart')
        if not chart or tool_result['status'] != 'ok':
            continue
        path = Path(chart['image_path']).resolve()
        if path.is_relative_to(PLOT_ROOT.resolve()) and path.is_file():
            st.image(str(path), caption=t('虚构演示数据 · ') + str(chart['point_count']) + t('个原始数据点 · 未插值或平滑'))
            for (label, key, mime) in [(t('下载PNG'), 'image_path', 'image/png'), (t('下载SVG'), 'svg_path', 'image/svg+xml'), (t('下载数据JSON'), 'data_path', 'application/json')]:
                artifact = Path(chart[key]).resolve()
                if artifact.is_relative_to(PLOT_ROOT.resolve()) and artifact.is_file():
                    st.download_button(label, artifact.read_bytes(), file_name=artifact.name, mime=mime, key=tool_result['result_id'] + key)
            with st.expander(t('图表数据点')):
                for panel in chart['panels']:
                    st.caption(panel['indicator'] + ' · ' + panel['measurement_context'] + ' · ' + panel['unit'])
                    st.dataframe(panel['points'], hide_index=True)
    if result.get('fallback_facts'):
        st.warning(t('仅展示工具返回的事实，未采纳未通过校验的模型结论。'))
        st.json(result['fallback_facts'], expanded=False)
    trace = result.get('trace', [])
    if trace:
        with st.expander(t('查询过程')):
            names = {'query_health_data': t('健康数据查询'), 'query_medications': t('用药记录查询'), 'ask_user': t('补充条件'), 'search_health_documents': t('本地报告检索'), 'plot_health_trend': t('趋势图绘制')}
            for row in trace:
                st.write(f"{names.get(row['tool'], row['tool'])} · {row['status']}")
                st.json(row['arguments'], expanded=False)
    for tool_result in result.get('tool_results', []):
        data = tool_result.get('data', {})
        if not data.get('windows'):
            continue
        with st.expander(t('比较窗口与样本 · 工具事实'), expanded=True):
            st.caption(t('区间左闭右开：开始日包含，结束日不含；事件当天不计入任一窗口。'))
            st.dataframe([{t('窗口'): label, t('开始（含）'): data['windows'][side]['start_date'], t('结束（不含）'): data['windows'][side]['end_date_exclusive']} for (side, label) in [('before', t('调整前')), ('after', t('调整后'))]], hide_index=True)
            for comp in data.get('comparisons', []):
                from health_agent.plotting import LABELS
                st.caption(indicator_label(comp['indicator'], LANG) + ' · ' + comp['measurement_context'] + ' · ' + comp['unit'] + t('；前窗') + str(comp['before']['count']) + t('条，后窗') + str(comp['after']['count']) + t('条。'))
                if tool_result['status'] == 'ok':
                    st.write(t('工具均值：') + comp['before']['display']['mean'] + ' → ' + comp['after']['display']['mean'] + ' ' + comp['unit'] + t('；差值：') + comp['display']['delta'] + ' ' + comp['unit'] + '。')
                else:
                    st.caption(t('业务条件不足：不展示比较均值或变化结论。'))
    plotted_sources = {x['data']['chart']['source_result_id'] for x in result.get('tool_results', []) if x.get('data', {}).get('chart')}
    for tool_result in result.get('tool_results', []):
        data = tool_result.get('data', {})
        if tool_result['status'] == 'ok' and data.get('parameters', {}).get('mode') == 'series' and data.get('records') and (tool_result['result_id'] not in plotted_sources):
            with st.expander(t('查询记录明细'), expanded=True):
                fields = ('record_id', 'local_time', 'indicator', 'value', 'unit', 'measurement_context', 'measurement_id')
                st.dataframe([{k: row[k] for k in fields if k in row} for row in data['records']], hide_index=True)
    sources = {}
    for tool_result in result.get('tool_results', []):
        for evidence in tool_result['evidence']:
            sources[evidence['source_id']] = evidence
    if sources:
        with st.expander(t('数据来源')):
            for (identifier, evidence) in sources.items():
                st.write(identifier)
                st.json({t('原文位置'): evidence.get('locator'), t('内容'): evidence.get('record')}, expanded=False)
for entry in st.session_state.chat_history:
    with st.chat_message(entry['role']):
        if entry['role'] == 'assistant':
            render_answer(entry['result'])
        else:
            st.write(entry['content'])
choice_answer = None
pending = st.session_state.get('pending_payload')
if pending:
    st.info(t('任务已暂停，等待补充条件。可回答追问，或输入“取消任务”。'))
    if pending.get('choices'):
        labels = {'fasting': t('空腹'), 'postprandial': t('餐后'), 'random': t('随机'), 'unknown': t('条件未知'), 'latest': t('最近一次'), 'series': t('时间序列')}
        key = pending['pending_task_id'] + '_' + pending['missing_fields'][0]
        chosen = st.selectbox(t('选择条件或事件'), pending['choices'], format_func=lambda x: labels.get(x, x), key='choice_' + key)
        if st.button(t('提交选择'), key='submit_' + key):
            choice_answer = chosen
question = st.chat_input(t('回答上一个追问，或输入“取消任务”') if pending else t('输入指标、用药、报告或趋势图问题'), max_chars=4000, disabled=not readiness['configured'] or not database_ready)
question = choice_answer or question
if question:
    st.session_state.chat_history.append({'role': 'user', 'content': question})
    with st.chat_message('user'):
        st.write(question)
    with st.chat_message('assistant'):
        with st.status(t('正在理解问题…'), expanded=True) as status:

            def event_sink(event):
                if event['event'] == 'tool_started':
                    label = {'query_medications': t('正在查询用药记录…'), 'query_health_data': t('正在查询健康指标…'), 'search_health_documents': t('正在检索本地报告…'), 'plot_health_trend': t('正在绘制趋势图…'), 'ask_user': t('等待补充条件…')}.get(event['tool'], t('正在执行工具…'))
                    status.update(label=label)
                elif event['event'] == 'model_retry':
                    status.update(label=t('服务连接异常，正在重试一次…'))
            agent = None
            try:
                agent = app_support.create_session_agent(session_id=st.session_state.session_id, event_sink=event_sink)
                result = agent.run(question)
            except ModelConfigError as exc:
                result = {'status': 'error', 'answer': 'Model configuration is not ready. Check the local settings.' if LANG == 'en' else str(exc), 'trace': [], 'tool_results': []}
            except Exception:
                result = {'status': 'error', 'answer': t('本轮执行失败，请检查配置和本地运行状态后重试。'), 'trace': [], 'tool_results': []}
            finally:
                if agent is not None and hasattr(agent, 'close'):
                    agent.close()
            st.session_state.pending_payload = result.get('pending')
            status.update(label={'no_data': t('该范围无数据'), 'unsupported': t('请求不支持'), 'insufficient_data': t('样本或窗口不足'), 'conflicting_events': t('比较窗口存在其他变更')}.get(result.get('business_status')) or t('等待补充条件') if result.get('business_status') or result['status'] == 'needs_clarification' else t('本轮完成') if result['status'] != 'error' else t('本轮未完成'), state='complete' if result['status'] != 'error' else 'error', expanded=False)
        render_answer(result)
        st.session_state.chat_history.append({'role': 'assistant', 'result': result})
    st.rerun()
