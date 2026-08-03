import argparse
from datetime import datetime
import os,re
from collections import Counter,defaultdict
from dataclasses import dataclass,field
from enum import IntEnum,auto
from pathlib import Path
from typing import ClassVar,NamedTuple,Any,Callable,TypeVar
T=TypeVar('T')
def getenv(name,convert,default):
	value=os.getenv(name)
	if value is None:return default
	return convert(value)
def to_bool(s):
	s=s.lower()
	if s in{'1','true','yes','on'}:return True
	if s in{'0','false','no','off'}:return False
	raise ValueError(s)
INPUT_FILE=getenv('INPUT_FILE',str,'tests/fixtures/dump.sql')
OUTPUT_PATH=getenv('OUTPUT_PATH',str,'tests/output/splitted_dump/')
SMART_MODE=getenv('SMART_MODE',to_bool,True)
VERBOSE=getenv('VERBOSE',to_bool,False)
REPORT=getenv('REPORT',to_bool,True)
SAVE_DUMP_DATA=getenv('SAVE_DUMP_DATA',to_bool,True)
FLUSH_TOC=getenv('FLUSH_TOC',to_bool,False)
TRIM_EMPTY_LINES=getenv('TRIM_EMPTY_LINES',to_bool,True)
APPEND_LINES_SEPARATOR=getenv('APPEND_LINES_SEPARATOR',int,0)
FORCE_DELETE_OUTPUT=getenv('FORCE_DELETE_OUTPUT',to_bool,True)
@dataclass(slots=True)
class SplitConfig:
	input_file:Path;output_path:Path;smart_mode:bool=True;verbose:bool=False;report:bool=True;save_dump_data:bool=True;flush_toc:bool=False;trim_empty_lines:bool=True;append_lines_separator:int=3
	@classmethod
	def from_args(cls,args):fields={f.name for f in cls.__dataclass_fields__.values()};filtered_args={k:v for(k,v)in vars(args).items()if k in fields};return cls(**filtered_args)
@dataclass(slots=True)
class TOCEntry:
	_name:str|None=field(default=None,init=False);type:str|None=field(default=None,init=False);schema:str|None=field(default=None,init=False);owner:str|None=field(default=None,init=False);tablespace:str|None=field(default=None,init=False);smart_mode:bool=field(default=True,init=True);OBJ_NAME:ClassVar[re.Pattern]=re.compile('^(?:"(.*)"|(.+?))(?:\\(.*\\))?$');ACL_OBJ_NAME:ClassVar[re.Pattern]=re.compile('^([A-Z ]+) ((?:".*"|.+?))(?:\\(.*\\))?$')
	@staticmethod
	def _truncate_utf8_to_bytes(text,max_bytes=63):
		if not text:return text
		encoded=text.encode('utf-8')
		if len(encoded)<=max_bytes:return text
		return encoded[:max_bytes].decode('utf-8',errors='ignore')
	@property
	def name(self):
		if not self._name:return self._name
		if not self.smart_mode:return self._truncate_utf8_to_bytes(self._name.split('(')[0])
		if(m:=self.OBJ_NAME.match(self._name)):return self._truncate_utf8_to_bytes(m[1]or m[2])
		return self._truncate_utf8_to_bytes(self._name)
	@property
	def acl_type_name(self):
		if self.type!='ACL'or not self._name:return
		if self.smart_mode:
			if(m:=self.ACL_OBJ_NAME.match(self._name)):return m[1],self._truncate_utf8_to_bytes(m[2]or m[3])
		return self.type,self.name
	def clear(self):self._name=None;self.type=None;self.schema=None;self.owner=None;self.tablespace=None
@dataclass(slots=True)
class TOCParser:
	class EState(IntEnum):NONE=0;BEGIN=auto();ENTRY=auto();DEPS=auto();META=auto();COMPLETE=auto();END=auto();FINISH=auto()
	COMMENT:ClassVar[str]='--';START:ClassVar[str]=COMMENT;CORE:ClassVar[list[dict[str,Any]]]=[{'state':EState.ENTRY,'startswith':'-- TOC entry ','regex':re.compile('^-- TOC entry \\d+ \\(class \\d+\\sOID \\d+\\)'),'required':False},{'state':EState.DEPS,'startswith':'-- Dependencies: ','regex':re.compile('^-- Dependencies:(?: \\d+)*'),'required':False},{'state':EState.META,'startswith':'-- Name: ','contains':['; Type: ','; Schema: ','; Owner: '],'regex':re.compile('^-- Name: (.*?); Type: (.*?); Schema: (.*?); Owner: (.*?)(?:; Tablespace: ([^\\n\\r]+))?$'),'required':True}];COMPLETE:ClassVar[list]=['-- PostgreSQL database dump complete','-- PostgreSQL database cluster dump complete'];END:ClassVar[str]=COMMENT;FINISH:ClassVar[str]='';smart_mode:bool=field(default=True,init=True);appends_empty_line:bool=field(default=False,init=True);buffer:list[str]=field(default_factory=list,init=False);toc_entry:TOCEntry=field(default=None,init=False);type_counter:Counter=field(default_factory=Counter,init=False);strictly_format:bool=field(default=False,init=True);has_meta:bool=field(default=False,init=False);has_complete:bool=field(default=False,init=False);_state:EState=field(default=EState.NONE,init=False)
	def __post_init__(self):
		if self.toc_entry is None:self.toc_entry=TOCEntry(smart_mode=self.smart_mode)
	@property
	def state(self):return self._state
	@property
	def is_completed_toc(self):return self._state==self.EState.FINISH
	def append(self,line):
		if line==self.FINISH:return self._set_state_and_append(self.EState.FINISH,line)
		if line==self.COMMENT:return self._handle_comment_only(line)
		if line.startswith(self.COMMENT):return self._handle_meta_line(line)
		return False
	def _is_state_allowed_for_meta(self):return self.EState.BEGIN<=self.state<self.EState.END
	def _is_complete(self,line):return line in self.COMPLETE
	def _handle_comment_only(self,line):
		if self.state<=self.EState.BEGIN:return self._set_state_and_append(self.EState.BEGIN,line)
		if self.state<=self.EState.END:return self._set_state_and_append(self.EState.END,line)
		return False
	def _handle_meta_line(self,line):
		if not self._is_state_allowed_for_meta():return False
		handlers=[self._try_handle_complete,self._try_match_core,self._try_fallback]
		for handler in handlers:
			result=handler(line)
			if result is not None:return result
		return False
	def _try_handle_complete(self,line):
		if self._is_complete(line):
			if self._set_state_and_append(self.EState.COMPLETE,line):self.has_complete=True;return True
			return False
	def _try_fallback(self,line):
		if not self.strictly_format:return self._append_to_buffer(line)
	def _try_match_core(self,line):
		for core in self.CORE:
			if line.startswith(core['startswith']):
				if self.smart_mode or core['state']==self.EState.META:
					if(match:=core['regex'].match(line)):return self._apply_core_pattern(core,match,line)
	def _apply_core_pattern(self,core,match,line):
		if not self._set_state_and_append(core['state'],line):return False
		if core['state']==self.EState.META:self._update_toc_entry(match);self.has_meta=True
		return True
	def _update_toc_entry(self,match):self.toc_entry._name=match.group(1);self.toc_entry.type=match.group(2);self.toc_entry.schema=match.group(3);self.toc_entry.owner=match.group(4);self.toc_entry.tablespace=match.group(5)
	def _set_state_and_append(self,new_state,line):
		if self._is_state_transition_invalid(new_state):return False
		self._state=new_state;self._inc_type();return self._append_to_buffer(line)
	def _append_to_buffer(self,line):
		if line or self.appends_empty_line:self.buffer.append(line)
		return True
	def _is_state_transition_invalid(self,new_state):
		if new_state<=self.state and self.strictly_format:return True
		if new_state==self.EState.FINISH and self.state!=self.EState.END:return True
		if new_state==self.EState.END and not self.has_meta and not self.has_complete:return True
		return False
	def _inc_type(self):
		if self.state==self.EState.FINISH and self.has_meta and self.toc_entry and self.toc_entry.type:self.type_counter[self.toc_entry.type]+=1
	def clear_current(self):self.buffer.clear();self.has_meta=False;self.has_complete=False;self._state=self.EState.NONE;self.toc_entry.clear()
class SplitResult(NamedTuple):lines_count:int;toc_count:int;created_dirs_count:int;written_files_count:int;appended_files_count:int;written_lines_count:int;type_counter:Counter;start_io:datetime;dir_io_finish:datetime;files_io_finish:datetime
def split_pg_dump(conf):
	lines_count=0;toc_count=0;created_dirs_count=0;written_files_count=0;appended_files_count=0;written_lines_count=0;conf.output_path.mkdir(parents=True,exist_ok=True);dirs_to_make=set();file_buffers=defaultdict(list);DUMP_OUTPUT_FILE='dump_data.sql';DUMP_OUTPUT=conf.output_path/DUMP_OUTPUT_FILE;active_file=DUMP_OUTPUT;current_content=[]
	def flush_current_to_buffer(file_path):
		if not current_content:return
		nonlocal conf
		if not active_file:
			print('Потерян блок данных!')
			if conf.verbose:print(current_content)
			current_content.clear();return
		if file_path.name==DUMP_OUTPUT_FILE:
			if conf.save_dump_data:file_buffers[file_path].extend(current_content)
			current_content.clear();return
		start=0;end=len(current_content)
		if conf.trim_empty_lines:
			while start<end and not current_content[start]:start+=1
			while end>start and not current_content[end-1]:end-=1
		if start<end:
			if file_path in file_buffers:
				nonlocal appended_files_count;appended_files_count+=1
				if conf.append_lines_separator>0:file_buffers[file_path].extend(['']*conf.append_lines_separator)
				file_buffers[file_path].extend(current_content[start:end])
			else:file_buffers[file_path]=current_content[start:end]
		current_content.clear()
	toc=TOCParser(smart_mode=conf.smart_mode,appends_empty_line=not conf.trim_empty_lines)
	with open(conf.input_file,'r',encoding='utf-8')as f:
		for line in f:
			lines_count+=1;line_str=line.rstrip('\r\n')
			if toc.append(line_str):
				if toc.is_completed_toc:
					toc_count+=1;flush_current_to_buffer(active_file)
					if conf.flush_toc:current_content.extend(toc.buffer);flush_current_to_buffer(DUMP_OUTPUT)
					if toc.has_complete:active_file=DUMP_OUTPUT;toc.clear_current();continue
					if toc.toc_entry.type=='ACL':obj_type,obj_name=toc.toc_entry.acl_type_name
					else:obj_type,obj_name=toc.toc_entry.type,toc.toc_entry.name
					obj_schema=toc.toc_entry.schema or'-';obj_type=obj_type or'-';obj_name=obj_name or'-';trans_table=str.maketrans({'"':'_',' ':'_',':':'_','/':'_','\\':'_','*':'_','?':'_','|':'_'});obj_schema=obj_schema.translate(trans_table);obj_type=obj_type.translate(trans_table);obj_name=obj_name.translate(trans_table);obj_type=obj_type.lower();dir_path=conf.output_path/obj_schema/obj_type;dirs_to_make.add(dir_path);active_file=dir_path/f"{obj_name}.sql";toc.clear_current()
			else:
				if toc.buffer:current_content.extend(toc.buffer);toc.clear_current()
				current_content.append(line_str)
	flush_current_to_buffer(active_file);start_io=datetime.now()
	if conf.verbose:print('Создание каталогов...')
	created_dirs_count=len(dirs_to_make)
	for d in dirs_to_make:d.mkdir(parents=True,exist_ok=True)
	dir_io_finish=datetime.now()
	if conf.verbose:print('Запись файлов...')
	for(file_path,lines)in file_buffers.items():written_files_count+=1;written_lines_count+=len(lines);file_path.write_text('\n'.join(lines),encoding='utf-8')
	files_io_finish=datetime.now();return SplitResult(lines_count=lines_count,toc_count=toc_count,created_dirs_count=created_dirs_count,written_files_count=written_files_count,appended_files_count=appended_files_count,written_lines_count=written_lines_count,type_counter=toc.type_counter,start_io=start_io,dir_io_finish=dir_io_finish,files_io_finish=files_io_finish)
def parse_args():parser=argparse.ArgumentParser(description='PostgreSQL dump splitter',epilog='Пример: python splitter.py dump.sql output/ --verbose',formatter_class=argparse.ArgumentDefaultsHelpFormatter);positional=parser.add_argument_group('Позиционные аргументы');positional.add_argument('input_file',nargs='?',type=Path,default=os.getenv('INPUT_FILE',INPUT_FILE),help='Путь к файлу дампа (env: INPUT_FILE)');positional.add_argument('output_path',nargs='?',type=Path,default=OUTPUT_PATH,help='Каталог для сохранения результатов (env: OUTPUT_PATH)');modes=parser.add_argument_group('Режимы работы');modes.add_argument('--smart-mode',dest='smart_mode',action=argparse.BooleanOptionalAction,default=SMART_MODE,help='Умный парсинг: ACL объекты сохраняются в файлы объектов, не в отдельные (при возможности) (env: SMART_MODE)');modes.add_argument('--verbose',action='store_true',default=VERBOSE,help='Выводить подробный лог (env: VERBOSE)');reports=parser.add_argument_group('Отчёты');reports.add_argument('--report',action=argparse.BooleanOptionalAction,default=REPORT,help='Показать подробную статистику после завершения (env: REPORT)');data=parser.add_argument_group('Обработка данных');data.add_argument('--save-dump-data',dest='save_dump_data',action=argparse.BooleanOptionalAction,default=SAVE_DUMP_DATA,help='Сохранять служебный файл dump_data.sql (env: SAVE_DUMP_DATA)');data.add_argument('--flush-toc',dest='flush_toc',action=argparse.BooleanOptionalAction,default=FLUSH_TOC,help='Записывать TOC-заголовки в dump_data.sql (требует --save-dump-data) (env: FLUSH_TOC)');formatting=parser.add_argument_group('Форматирование');formatting.add_argument('--trim-empty-lines',dest='trim_empty_lines',action=argparse.BooleanOptionalAction,default=TRIM_EMPTY_LINES,help='Удалять пустые строки в начале и конце блоков кода (env: TRIM_EMPTY_LINES)');formatting.add_argument('--append-lines-separator',dest='append_lines_separator',type=int,default=APPEND_LINES_SEPARATOR,help='Количество пустых строк между блоками кода при дозаписи в файл (env: APPEND_LINES_SEPARATOR)');additional=parser.add_argument_group('Дополнительно');additional.add_argument('--force-delete-output',dest='force_delete_output',action='store_true',default=FORCE_DELETE_OUTPUT,help='Принудительное удаление output каталога, если каталог не пустой (env: FORCE_DELETE_OUTPUT)');return parser.parse_args()
if __name__=='__main__':
	args=parse_args()
	if args.output_path.exists()and any(args.output_path.iterdir()):
		if args.force_delete_output:
			print(f'Очитка каталога "{args.output_path}"');import shutil
			if args.output_path.exists():shutil.rmtree(args.output_path)
		else:print(f'Каталог "{args.output_path}" не пустой');exit(1)
	start_at=datetime.now()
	if args.verbose:print('Переменные окружения:');print(f"  Input: {args.input_file}");print(f"  Output: {args.output_path}");print(f"  Smart mode: {args.smart_mode}");print(f"  Verbose: {args.verbose}");print(f"  Report: {args.report}");print(f"  Save dump file: {args.save_dump_data}");print(f"  Flush TOC: {args.flush_toc}");print(f"  Trim empty lines: {args.trim_empty_lines}");print(f"  Append lines separator: {args.append_lines_separator}")
	print('Начало разделения дампа...');config=SplitConfig.from_args(args);res=split_pg_dump(config)
	if args.verbose:print('-'*30)
	print(f"Разделение завершено за {datetime.now()-start_at}")
	if args.report:
		print(f"  📋 Обработано строк: {res.lines_count:_}");print(f"     Обнаружено TOC (включая технические): {res.toc_count:_}");total=sum(res.type_counter.values())
		for(key,count)in res.type_counter.most_common():percentage=count/total*100 if total>0 else 0;bar='█'*int(count/max(res.type_counter.values())*30);print(f"  {key:<20} {count:>6} ({percentage:>5.1f}%) {bar}")
		print(f"  {"---ИТОГО---":<20} {total:_}");print(f"\n📁 Создано каталогов: {res.created_dirs_count:_}");print(f"📊 Записано файлов: {res.written_files_count:_}");print(f"   Дозаписей в файлы: {res.appended_files_count:_}");print(f"   Записано строк: {res.written_lines_count:_}");print('\n🕐 Время обработки:');print(f"   Парсинг данных:     {res.start_io-start_at}");print(f"   Создание каталогов: {res.dir_io_finish-res.start_io}");print(f"   Запись файлов:      {res.files_io_finish-res.dir_io_finish}")