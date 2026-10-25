"""Application-owned language selection and labels; evidence is never machine-translated."""
import re
DRUGS={'二甲双胍':'metformin','氨氯地平':'amlodipine','阿托伐他汀':'atorvastatin'}
INDICATORS={'glucose':'Blood glucose','weight':'Weight','systolic_blood_pressure':'Systolic BP','diastolic_blood_pressure':'Diastolic BP','blood_pressure':'Blood pressure'}
CONDITIONS={'fasting':'Fasting','postprandial':'Postprandial','random':'Random','unknown':'Unknown'}

def language_for(text,previous='zh',preferred=None):
    if preferred in ('zh','en'):return preferred
    if re.search(r'(?:用英文|英语回答|answer in english|reply in english)',text,re.I):return 'en'
    if re.search(r'(?:用中文|中文回答|answer in chinese|reply in chinese)',text,re.I):return 'zh'
    if text.strip().lower() in ('fasting','postprandial','random','unknown','latest','series') or re.fullmatch(r'(?:E\d+|\d+)',text.strip(),re.I):return previous
    natural=text
    for term in ['低密度脂蛋白胆固醇','低密度脂蛋白',*DRUGS.keys()]:natural=natural.replace(term,'')
    if re.search(r'[\u4e00-\u9fff]',natural):return 'zh'
    return 'en' if re.search(r'[A-Za-z]{2,}',text) else previous

def english_medication(name):return DRUGS.get(name,name)
def canonical_medication(name):
    if not isinstance(name,str):return name
    return next((zh for zh,en in DRUGS.items() if en.casefold()==name.strip().casefold()),name)
def indicator_label(key,language='zh'):
    if language=='en':return INDICATORS.get(key,key)
    return {'glucose':'血糖','weight':'体重','systolic_blood_pressure':'收缩压','diastolic_blood_pressure':'舒张压','blood_pressure':'血压'}.get(key,key)

def wrong_language(text,language):
    body=re.sub(r'```[\s\S]*?```|(?m:^>.*$)','',text)
    for term in ['低密度脂蛋白胆固醇','低密度脂蛋白',*DRUGS.keys(),'空腹','餐后','随机','条件未知']:body=body.replace(term,'')
    han=len(re.findall(r'[\u4e00-\u9fff]',body));words=len(re.findall(r'\b[A-Za-z]{2,}\b',body))
    return (han>2 or (han>0 and words<4)) if language=='en' else (not han and words>=8)

ERRORS={
 'invalid_question':'Enter a non-empty question of at most 4000 characters.',
 'unverified_facts':'The answer failed factual verification. No conclusion was accepted; inspect the tool facts and original sources.',
 'invalid_final_response':'The answer failed format or source verification. Tool results remain available.',
 'unsupported_absence_claim':'The answer exceeded the retrieved evidence scope. Inspect the original excerpts.',
 'chart_not_completed':'The chart task was not completed. The query results remain available.',
 'language_mismatch':'The answer did not match the requested language. No unverified translation was displayed.',
 'tool_correction_limit':'The tool parameters could not be corrected within the allowed limit.',
 'invalid_ask_user':'The clarification request was invalid; this turn was stopped.',
 'model_call_limit':'The model-call limit was reached. Please narrow the question.',
 'tool_call_limit':'The tool-call limit was reached. Please narrow the question.',
}
def english_error(code):return ERRORS.get(code,'This turn could not be completed. Check the local service and configuration; available tool facts have been retained.')

CATALOG={
'个人健康数据分析 Agent':'Personal Health Data Agent','运行状态':'Runtime status','当前会话支持承接问题；刷新页面可能开始新会话。':'Follow-up questions share this session. Reloading may start a new session.',
'已准备':'Ready','需要初始化':'Initialization required','已配置':'Configured','待配置':'Not configured','本机 Ollama 推理 · 无需 API 密钥':'Local Ollama inference · No API key required','新建会话':'New session','报告索引':'Report index',
'上传虚构TXT报告':'Upload a synthetic TXT report','UTF-8 TXT · 每份最多1MB · 总计最多5份 · 日期按报告日期筛选':'UTF-8 TXT · Up to 1MB per file · 5 reports total · Filtered by report date','选择TXT':'Choose TXT','报告日期':'Report date','确认文件仅含虚构演示数据':'Confirm this file contains only synthetic demo data','上传并建立索引':'Upload and index','正在本机解析和向量化…':'Parsing and embedding locally…','报告已登记，但索引失败；请检查本地embedding服务后重建索引。':'The report was registered, but indexing failed. Check local embeddings and rebuild.','上传失败：请检查UTF-8编码、文件大小、报告日期和总份数。':'Upload failed. Check UTF-8 encoding, size, report date and report count.','重建报告索引':'Rebuild report index','在本机重建报告索引…':'Rebuilding the local report index…','报告索引重建完成；刷新可更新状态。':'Report index rebuilt. Reload to refresh status.','部分报告索引失败，尚未可检索。':'Some reports failed indexing and are not ready for semantic retrieval.','示例问题':'Example questions',
'我最近一次血糖是多少？':'What is my latest blood glucose?','画出2026年10月1日至23日的体重趋势。':'Plot my weight from October 1 to October 23, 2026.','去年的体检报告有没有提到LDL？':"Did last year's checkup reports mention LDL?",'R003报告对血糖记录整理是怎么说的？':'What does report R003 say about organizing blood glucose records?','我当前有哪些用药记录？':'What medications am I currently taking?','比较2026年10月1日二甲双胍调整前后14天的空腹血糖。':'Compare fasting blood glucose in the 14 days before and after the metformin change on October 1, 2026.',
'数据库尚未初始化，请先运行 python -m health_agent init。':'Database not initialized. Run python -m health_agent init first.','记录查询回执 · 明细直接来自工具，未由模型改写':'Record-query receipt · Original tool records, not model-rewritten values','无数据':'No data','不支持':'Unsupported','样本或窗口不足':'Insufficient samples or incomplete window','窗口内有其他变更':'Other changes in the comparison window','图表工具回执 · 日期、点数与单位来自查询快照':'Chart receipt · Dates, point count and units come from the query snapshot','本轮未完成，可查看已执行的查询。':'This turn was not completed. Executed queries remain available.','下载PNG':'Download PNG','下载SVG':'Download SVG','下载数据JSON':'Download data JSON','图表数据点':'Chart data points','仅展示工具返回的事实，未采纳未通过校验的模型结论。':'Only tool facts are shown. The unverified model conclusion was not accepted.','查询过程':'Query trace','健康数据查询':'Health-record query','用药记录查询':'Medication query','补充条件':'Clarification','本地报告检索':'Local report retrieval','趋势图绘制':'Chart generation','比较窗口与样本 · 工具事实':'Comparison windows and samples · Tool facts','区间左闭右开：开始日包含，结束日不含；事件当天不计入任一窗口。':'Start included, end excluded. The medication-event day is excluded from both windows.','窗口':'Window','开始（含）':'Start (inclusive)','结束（不含）':'End (exclusive)','调整前':'Before','调整后':'After','业务条件不足：不展示比较均值或变化结论。':'Business conditions are insufficient: comparison means and change conclusions are not shown.','查询记录明细':'Original query records','数据来源':'Sources','原文位置':'Original location','内容':'Content','任务已暂停，等待补充条件。可回答追问，或输入“取消任务”。':'Task paused. Answer the clarification or enter "cancel task".','空腹':'Fasting','餐后':'Postprandial','随机':'Random','条件未知':'Unknown','最近一次':'Latest','时间序列':'Time series','选择条件或事件':'Choose a condition or event','提交选择':'Submit choice','回答上一个追问，或输入“取消任务”':'Answer the clarification, or enter "cancel task"','输入指标、用药、报告或趋势图问题':'Ask about a measurement, medication, report or chart','正在理解问题…':'Understanding the question…','正在查询用药记录…':'Querying medication records…','正在查询健康指标…':'Querying health records…','正在检索本地报告…':'Searching local reports…','正在绘制趋势图…':'Generating the chart…','等待补充条件…':'Waiting for clarification…','正在执行工具…':'Running a tool…','服务连接异常，正在重试一次…':'Connection failed; retrying once…','本轮执行失败，请检查配置和本地运行状态后重试。':'This turn failed. Check configuration and local services, then retry.','该范围无数据':'No data in this range','请求不支持':'Unsupported request','比较窗口存在其他变更':'Other changes in the comparison window','等待补充条件':'Waiting for clarification','本轮完成':'Turn completed','本轮未完成':'Turn not completed',
'当前支持指标查询、用药记录和前后比较。支持同一会话追问恢复与条件承接；已支持本地报告检索与原文引用；已支持趋势图；图表使用查询得到的原始记录。':'Query health records and medications, compare event windows, resume clarifications, retrieve local reports and plot original records. Replies follow the question language; existing messages and source quotes retain their original language.'}
FRAGMENTS={'模拟数据 · 参考日期 ':'Synthetic data · Reference date ','会话：':'Session: ','数据：':'Data: ','模型：':'Model: ','内容重复，使用已有报告：':'Duplicate content; using report: ','已可检索：':'Ready for retrieval: ','工具业务回执 · ':'Business receipt · ','虚构演示数据 · ':'Synthetic demo data · ','个原始数据点 · 未插值或平滑':' original points · No interpolation or smoothing','；前窗':'; before: ','条，后窗':' records; after: ','条。':' records.','工具均值：':'Tool means: ','；差值：':'; difference: '}
def ui_text(text,language='zh'):
    if language!='en':return text
    if text in CATALOG:return CATALOG[text]
    for a,b in FRAGMENTS.items():text=text.replace(a,b)
    return text
