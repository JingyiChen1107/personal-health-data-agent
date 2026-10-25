"""Validate explicit user calendar bounds after tool selection, before SQL execution."""
from datetime import date,timedelta
import re

def _legacy_bounds(text,reference):
    match=re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日?\s*(?:到|至|~|～|—)\s*(?:(\d{4})年)?(?:(\d{1,2})月)?(\d{1,2})日',text)
    if match:
        y,m,d,ey,em,ed=match.groups();left=date(int(y),int(m),int(d));year=int(ey or y);month=int(em or m)
        if not ey and em and month<int(m):year+=1
        right=date(year,month,int(ed))
        if right<left:raise ValueError('invalid user date range')
        return {'start_date':left.isoformat(),'end_date':(right+timedelta(days=1)).isoformat()}
    match=re.search(r'(\d{4}-\d{2}-\d{2})\s*(?:到|至|~|～|—)\s*(\d{4}-\d{2}-\d{2})',text)
    if match:
        left,right=map(date.fromisoformat,match.groups())
        if right<left:raise ValueError('invalid user date range')
        return {'start_date':left.isoformat(),'end_date':(right+timedelta(days=1)).isoformat()}
    if not any(w in text for w in ['以来','之后','之前','以前','以后','截至','截止','到今天','至今','since','before','after','until']):
        days=re.findall(r'(\d{4})年(\d{1,2})月(\d{1,2})日',text)
        iso=re.findall(r'\d{4}-\d{2}-\d{2}',text)
        if len(days)==1:
            day=date(*map(int,days[0]));return {'start_date':day.isoformat(),'end_date':(day+timedelta(days=1)).isoformat()}
        if len(iso)==1:
            day=date.fromisoformat(iso[0]);return {'start_date':day.isoformat(),'end_date':(day+timedelta(days=1)).isoformat()}
    if not re.search(r'\d{1,2}月|\d{1,2}日',text):
        years=re.findall(r'(\d{4})年',text)
        if len(years)==1:
            y=int(years[0]);return {'start_date':date(y,1,1).isoformat(),'end_date':date(y+1,1,1).isoformat()}
        if not years and '去年' in text and '今年' not in text:
            return {'start_date':date(reference.year-1,1,1).isoformat(),'end_date':date(reference.year,1,1).isoformat()}
    return None


MONTHS={name.lower():i for i,name in enumerate(('January','February','March','April','May','June','July','August','September','October','November','December'),1)}
MONTHS.update({name[:3]:value for name,value in list(MONTHS.items())})
MONTH_PATTERN='(?:'+ '|'.join(sorted(MONTHS,key=len,reverse=True))+')'

def english_dates(text,reference_year):
    def replace(m):
        month,day,year=m.groups();return date(int(year or reference_year),MONTHS[month.lower()],int(day)).isoformat()
    return re.sub(r'\b('+MONTH_PATTERN+r')\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(20\d{2}))?\b',replace,text,flags=re.I)

def mentioned_dates(text,reference):
    normalized=english_dates(text,reference.year)
    return set(re.findall(r'\d{4}-\d{2}-\d{2}',normalized)) | {date(*map(int,x)).isoformat() for x in re.findall(r'(\d{4})年(\d{1,2})月(\d{1,2})日',text)}

def explicit_bounds(text,reference):
    # English date arithmetic is application-owned, just like Chinese inclusive ranges.
    pattern=r'\b('+MONTH_PATTERN+r')\.?\s+(\d{1,2})(?:,?\s+(20\d{2}))?\s*(?:to|through|–|—|-)\s*(?:('+MONTH_PATTERN+r')\.?\s+)?(\d{1,2})(?:,?\s+(20\d{2}))?\b'
    m=re.search(pattern,text,re.I)
    if m:
        lm,ld,ly,rm,rd,ry=m.groups();y=int(ly or ry or reference.year);left=date(y,MONTHS[lm.lower()],int(ld));right=date(int(ry or y),MONTHS[(rm or lm).lower()],int(rd))
        if right<left:raise ValueError('invalid user date range')
        return {'start_date':left.isoformat(),'end_date':(right+timedelta(days=1)).isoformat()}
    normalized=english_dates(text,reference.year)
    match=re.search(r'(\d{4}-\d{2}-\d{2})\s*(?:to|through)\s*(\d{4}-\d{2}-\d{2})',normalized,re.I)
    if match:
        left,right=map(date.fromisoformat,match.groups())
        if right<left:raise ValueError('invalid user date range')
        return {'start_date':left.isoformat(),'end_date':(right+timedelta(days=1)).isoformat()}
    if re.search(r'\blast year\b',text,re.I):return {'start_date':date(reference.year-1,1,1).isoformat(),'end_date':date(reference.year,1,1).isoformat()}
    m=re.search(r'\b(?:in|during|calendar year)\s+(20\d{2})\b',text,re.I)
    if m:
        y=int(m.group(1));return {'start_date':date(y,1,1).isoformat(),'end_date':date(y+1,1,1).isoformat()}
    if re.search(r'\b(?:past|last)\s+(?:30 days|month)\b',text,re.I):return {'start_date':(reference-timedelta(days=29)).isoformat(),'end_date':(reference+timedelta(days=1)).isoformat()}
    return _legacy_bounds(normalized,reference)
