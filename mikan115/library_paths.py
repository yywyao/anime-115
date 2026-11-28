"""Journaled rename of the managed download root, preserving file identities."""
import contextlib
import ctypes
import errno
import json
import os
from pathlib import Path
import sys
import time

from .errors import AppError

STORAGE_ROOT_NAME='anime'


def rename_directory(source,target):
    """Rename on the same filesystem without replacing an existing target."""
    if os.name=='nt':os.rename(source,target);return
    if sys.platform.startswith('linux'):
        libc=ctypes.CDLL(None,use_errno=True);rename=getattr(libc,'renameat2',None)
        if rename is None:raise AppError('LIBRARY_RENAME_UNSUPPORTED','当前系统不支持安全迁移目录',409)
        rename.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint];rename.restype=ctypes.c_int
        if rename(-100,os.fsencode(source),-100,os.fsencode(target),1)!=0:
            error=ctypes.get_errno();raise OSError(error,os.strerror(error),str(target))
        return
    raise AppError('LIBRARY_RENAME_UNSUPPORTED','当前系统不支持安全迁移目录',409)


def migrate_library(library):
    store=library.store;root=library.root.resolve();old=root/'mikan';new=root/STORAGE_ROOT_NAME
    if store.setting('update_maintenance')=='1':raise AppError('UPDATE_IN_PROGRESS','更新中，请稍后再试',503)
    version='anime:'+str(root)
    if store.setting('library_directory_version')==version and not os.path.lexists(old) and not store.setting('library_root_rename'):return
    lock=getattr(library.workflow,'connection_lock',None)
    with lock if lock is not None else contextlib.nullcontext():
        if not root.is_relative_to(Path(store.data).resolve()) or old.resolve().parent!=root or new.resolve().parent!=root or old.is_symlink() or new.is_symlink():
            raise AppError('UNSAFE_LIBRARY_PATH','入库目录未通过校验',409)
        with store.transaction() as db:
            if db.execute("SELECT 1 FROM library_tasks WHERE status='DOWNLOADING' AND lease_until>? LIMIT 1",(time.time(),)).fetchone():
                raise AppError('LIBRARY_MIGRATION_BUSY','等待当前下载结束后迁移目录',409)
            saved=db.execute("SELECT value FROM settings WHERE key='library_root_rename'").fetchone()
            intent=json.loads(saved['value']) if saved else None
            if intent and (intent.get('old')!=str(old) or intent.get('new')!=str(new)):
                raise AppError('UNSAFE_LIBRARY_PATH','目录迁移记录不匹配',409)
            updates=[]
            for table,column in [('library_tasks','root_path'),('library_items','target_path')]:
                for row in db.execute(f'SELECT id,{column} FROM {table}'):
                    path=Path(row[column])
                    try:relative=path.relative_to(old)
                    except ValueError:continue
                    if '..' in relative.parts:raise AppError('UNSAFE_LIBRARY_PATH','入库路径未通过校验',409)
                    updates.append((table,column,row['id'],str(new/relative)))
            exists=os.path.lexists(old);target_exists=os.path.lexists(new)
            if exists and target_exists:raise AppError('LIBRARY_ROOT_CONFLICT','同时存在 mikan 和 anime 目录，请先处理重名目录',409)
            if exists:
                if not old.is_dir():raise AppError('UNSAFE_LIBRARY_PATH','原入库目录未通过校验',409)
                identity=old.stat()
                if intent and (intent['device']!=identity.st_dev or intent['inode']!=str(identity.st_ino)):
                    raise AppError('LIBRARY_ROOT_CHANGED','原入库目录已变化，已停止迁移',409)
                intent={'old':str(old),'new':str(new),'device':identity.st_dev,'inode':str(identity.st_ino)}
                db.execute("INSERT INTO settings VALUES('library_root_rename',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(json.dumps(intent),))
            elif intent:
                if not target_exists:raise AppError('LIBRARY_ROOT_CHANGED','迁移目录未找到，请检查存储',409)
            elif updates and target_exists:
                raise AppError('LIBRARY_ROOT_CONFLICT','旧路径与 anime 目录归属未确认，请检查存储',409)
        # Intent is durable before changing the directory entry. No file is
        # copied, renamed individually, adopted, deleted, or overwritten.
        if exists:
            try:rename_directory(old,new)
            except FileExistsError:raise AppError('LIBRARY_ROOT_CONFLICT','anime 目录已存在，未覆盖',409)
            except OSError:raise AppError('LIBRARY_ROOT_RENAME_FAILED','目录迁移暂未完成，请重试',409)
        elif not intent:new.mkdir(exist_ok=True)
        if intent:
            identity=new.stat()
            if identity.st_dev!=intent['device'] or str(identity.st_ino)!=intent['inode']:
                raise AppError('LIBRARY_ROOT_CHANGED','迁移目录已变化，已停止更新路径',409)
        with store.transaction() as db:
            for table,column,ident,path in updates:db.execute(f'UPDATE {table} SET {column}=? WHERE id=?',(path,ident))
            db.execute("DELETE FROM settings WHERE key='library_root_rename'")
            db.execute("INSERT INTO settings VALUES('library_directory_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(version,))
