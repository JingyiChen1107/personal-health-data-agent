"""Plots only application-owned query snapshots. No model-provided rows or paths."""
from dataclasses import dataclass
from datetime import datetime
import hashlib,json,math,os,threading
from pathlib import Path
from health_agent.config import PROJECT_ROOT
from health_agent.contracts import arguments,InputError
from health_agent.results import ToolResult

PLOT_ROOT=PROJECT_ROOT/'artifacts/charts'
RENDER_LOCK=threading.Lock()
LABELS={'weight':'体重','glucose':'血糖','systolic_blood_pressure':'收缩压','diastolic_blood_pressure':'舒张压'}
CONTEXTS={'fasting':'空腹','postprandial':'餐后','random':'随机','unknown':'条件未知'}
@dataclass(frozen=True)
class PlotQuery:
    result_id:str
    chart_type:str='line'
    @classmethod
    def parse(cls,raw):
        q=cls(**arguments(raw,cls))
        if not isinstance(q.result_id,str) or not q.result_id.startswith('res_') or len(q.result_id)>100:raise InputError('invalid_result_id','结果标识无效，请重新查询。')
        if q.chart_type not in ('line','scatter'):raise InputError('invalid_chart_type','只支持line或scatter。')
        return q

def plot(tools,raw,*,output_root=None):
    try:
        q=PlotQuery.parse(raw)
        try:source=tools.cache.get(q.result_id,context=tools.context,tool='query_health_data')
        except KeyError:raise InputError('invalid_result_id','结果不存在、已过期或不属于本会话，请重新查询。')
        data=source['data'];rows=data.get('records',[])
        if source['status']=='no_data':return ToolResult('no_data',data={'source_result_id':q.result_id,'point_count':0}).to_dict()
        if source['status']!='ok' or data.get('parameters',{}).get('mode') not in ('latest','series','summary'):
            raise InputError('unsupported_plot_source','只绘制成功的原始记录查询；比较结果请先另查时间序列。',status='unsupported')
        if not rows:return ToolResult('no_data',data={'source_result_id':q.result_id,'point_count':0}).to_dict()
        expected=data['parameters']['indicator'];permitted={'systolic_blood_pressure','diastolic_blood_pressure'} if expected=='blood_pressure' else {expected}
        groups={};seen=set()
        for row in rows:
            indicator=row['indicator'];cfg=tools.config.indicators.get(indicator)
            if (not cfg or indicator not in permitted or row['patient_id']!=tools.context.patient_id or row['unit']!=cfg['unit'] or row['measurement_context'] not in cfg['contexts']
                or row['record_id'] in seen or isinstance(row['value'],bool) or not math.isfinite(float(row['value'])) or float(row['value'])<0):
                raise InputError('invalid_plot_snapshot','快照的用户、指标、单位、条件或数据点无效。')
            seen.add(row['record_id']);utc=datetime.fromisoformat(row['measured_at']);local=datetime.fromisoformat(row['local_time'])
            if utc.tzinfo is None or local.tzinfo is None or utc!=local:raise InputError('invalid_plot_snapshot','快照时间不一致。')
            key=(indicator,row['measurement_context']);groups.setdefault(key,[]).append(row)
        if expected=='blood_pressure':tools._pair_records(rows)
        panels=[]
        for (indicator,context),records in sorted(groups.items()):
            records=sorted(records,key=lambda x:(x['measured_at'],x['record_id']))
            panels.append({'indicator':indicator,'measurement_context':context,'unit':records[0]['unit'],
                'line_drawn':q.chart_type=='line' and len(records)>1,'point_count':len(records),
                'points':[{'record_id':r['record_id'],'measurement_id':r['measurement_id'],'local_time':r['local_time'],'value':r['value']} for r in records]})
        identity=hashlib.sha256(json.dumps([tools.context.patient_id,tools.context.session_id,tools.context.reference_date.isoformat(),q.result_id,q.chart_type,panels,getattr(tools,'language','zh')],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        root=Path(output_root or PLOT_ROOT);folder=root/identity[:20];folder.mkdir(parents=True,exist_ok=True);os.chmod(folder,0o700)
        meta={'chart_id':'chart_'+identity[:20],'source_result_id':q.result_id,'chart_type':q.chart_type,'point_count':len(rows),
            'measurement_count':len({r['measurement_id'] or r['record_id'] for r in rows}),'record_ids':[r['record_id'] for r in rows],
            'date_range':{'first_local_time':min(r['local_time'] for r in rows),'last_local_time':max(r['local_time'] for r in rows)},
            'query_range':data.get('range'),'panels':panels,'synthetic_only':True,'language':getattr(tools,'language','zh'),'timezone':str(tools.config.timezone)}
        with RENDER_LOCK:
            if not all((folder/('chart.'+ext)).is_file() for ext in ('png','svg')):render(meta,folder)
            (folder/'data.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2))
        meta.update({'image_path':str(folder/'chart.png'),'svg_path':str(folder/'chart.svg'),'data_path':str(folder/'data.json')})
        warnings=[]
        if any(p['point_count']==1 for p in panels):warnings.append({'code':'single_point','message':'单点仅标记，不形成趋势线，不能据此判断趋势。'})
        if len({p['measurement_context'] for p in panels})>1:warnings.append({'code':'separate_context_panels','message':'不同血糖测量条件分别展示，不合并统计。'})
        return ToolResult('ok',data={'chart':meta},evidence=source['evidence'],warnings=warnings).to_dict()
    except InputError as exc:return ToolResult(exc.status,error={'code':exc.code,'message':str(exc)}).to_dict()
    except (OSError,ValueError,TypeError,KeyError):return ToolResult('error',error={'code':'plot_failed','message':'图表生成失败，未使用不完整的数据。'}).to_dict()

def render(meta,folder):
    from health_agent.i18n import INDICATORS,CONDITIONS
    english=meta.get('language')=='en'
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib import dates as mdates,rc_context,font_manager
    fonts=[Path('/System/Library/Fonts/PingFang.ttc'),Path('/System/Library/Fonts/STHeiti Light.ttc')]
    font=next((p for p in fonts if p.exists()),None)
    if font:font_manager.fontManager.addfont(str(font));family=font_manager.FontProperties(fname=str(font)).get_name()
    else:family='DejaVu Sans'
    with rc_context({'font.family':family,'axes.unicode_minus':False,'svg.fonttype':'none'}):
        fig=Figure(figsize=(9,max(3.6,2.8*len(meta['panels']))),layout='constrained',facecolor='white');FigureCanvasAgg(fig)
        axes=fig.subplots(len(meta['panels']),1,squeeze=False)
        colors=['#2563eb','#0f766e','#9333ea','#d97706']
        for index,(panel,axis) in enumerate(zip(meta['panels'],axes[:,0])):
            points=panel['points'];x=[datetime.fromisoformat(p['local_time']) for p in points];y=[p['value'] for p in points]
            axis.plot(x,y,marker='o',markersize=6,linewidth=1.8,linestyle='-' if panel['line_drawn'] else 'None',color=colors[index%len(colors)])
            for px,py in zip(x,y):axis.annotate(f'{py:g}',(px,py),xytext=(0,9),textcoords='offset points',ha='center',fontsize=9)
            label=(INDICATORS if english else LABELS)[panel['indicator']]+(' · '+(CONDITIONS if english else CONTEXTS)[panel['measurement_context']] if panel['indicator']=='glucose' else '')
            axis.set_title(label+(' ('+str(panel['point_count'])+' data points)' if english else '（'+str(panel['point_count'])+'个数据点）'),loc='left',fontsize=12,pad=16)
            axis.set_ylabel(panel['unit']);axis.set_xlabel(('Measurement time (' if english else '测量日期（')+meta['timezone']+(')' if english else '）'))
            axis.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=3,maxticks=7,tz=x[0].tzinfo))
            axis.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d\n%H:%M',tz=x[0].tzinfo))
            axis.grid(axis='y',alpha=.2);axis.spines[['top','right']].set_visible(False);axis.margins(y=.25)
            if len(x)==1:
                from datetime import timedelta
                axis.set_xlim(x[0]-timedelta(hours=12),x[0]+timedelta(hours=12))
                axis.text(.99,.04,('One point only; no trend can be determined' if english else '仅一个数据点，不能判断趋势'),transform=axis.transAxes,ha='right',fontsize=9,color='#475569')
        fig.suptitle(('Synthetic data · Original records (no interpolation or smoothing)' if english else '虚构演示数据 · 原始记录展示（未插值、未平滑）'),fontsize=12,color='#334155')
        for extension in ('png','svg'):
            temp=folder/('chart.tmp.'+extension);fig.savefig(temp,dpi=160,format=extension);os.chmod(temp,0o600);os.replace(temp,folder/('chart.'+extension))
        fig.clear()
