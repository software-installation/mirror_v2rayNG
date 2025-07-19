import os
import json
import requests
import datetime
import time
import traceback
from github import Github, GithubException

# 环境变量与配置
SOURCE_REPO = os.environ['SOURCE_REPO']
TARGET_REPO = os.environ.get('TARGET_REPO', os.environ['GITHUB_REPOSITORY'])
GITHUB_TOKEN = os.environ['GITHUB_TOKEN']
SOURCE_GITHUB_TOKEN = os.environ.get('SOURCE_GITHUB_TOKEN', GITHUB_TOKEN)
SYNCED_DATA_FILE = os.environ.get('SYNCED_DATA_FILE', 'synced_data.json')  # 同步状态文件路径（默认当前目录下synced_data.json）
SYNCED_DATA_BACKUP = f"{SYNCED_DATA_FILE}.bak"
SOURCE_OWNER, SOURCE_REPO_NAME = SOURCE_REPO.split('/')
RETRY_COUNT = int(os.environ.get('RETRY_COUNT', 3))  # 上传重试次数（默认3次）
RETRY_DELAY = int(os.environ.get('RETRY_DELAY', 10))  # 重试间隔（秒，默认10秒）

print(f"=== 配置信息 ===")
print(f"源仓库: {SOURCE_REPO}")
print(f"目标仓库: {TARGET_REPO}")


### 1. 同步状态文件管理
def load_synced_data():
    def _load(path):
        with open(path, 'r') as f:
            return json.load(f)
    
    try:
        if os.path.exists(SYNCED_DATA_FILE):
            return _load(SYNCED_DATA_FILE)
    except Exception as e:
        print(f"主文件损坏，尝试从备份恢复: {str(e)}")
        if os.path.exists(SYNCED_DATA_BACKUP):
            try:
                return _load(SYNCED_DATA_BACKUP)
            except Exception as e:
                print(f"备份文件也损坏: {str(e)}")
    
    return {'releases': {}, 'assets': {}, 'source_codes': {}}


def save_synced_data(data):
    temp_file = f"{SYNCED_DATA_FILE}.tmp"
    try:
        with open(temp_file, 'w') as f:
            json.dump(data, f, indent=2)
        if os.path.exists(SYNCED_DATA_FILE):
            os.replace(SYNCED_DATA_FILE, SYNCED_DATA_BACKUP)
        os.replace(temp_file, SYNCED_DATA_FILE)
        print(f"同步状态已保存（含备份）")
    except Exception as e:
        print(f"保存失败: {str(e)}")
        if os.path.exists(temp_file):
            os.remove(temp_file)


### 2. 核心工具函数
def get_asset_info(asset):
    """获取资产的时间和大小信息（统一转为UTC时间）"""
    if not asset:
        return None
    # 确保时间为UTC格式
    updated_at = asset.updated_at.astimezone(datetime.timezone.utc) if asset.updated_at else None
    return {
        'size': asset.size,
        'updated_at': updated_at.isoformat() if updated_at else None
    }


def delete_existing_asset(target_release, asset_name):
    """删除目标Release中同名的资产（解决422冲突）"""
    for asset in target_release.get_assets():
        if asset.name == asset_name:
            try:
                print(f"删除目标仓库中已存在的 {asset_name}")
                asset.delete_asset()
                return True
            except Exception as e:
                print(f"删除 {asset_name} 失败: {str(e)}")
    return False


def retry_upload(target_release, file_path, name, content_type):
    """带重试和冲突处理的上传函数"""
    for attempt in range(RETRY_COUNT):
        try:
            # 上传前先删除同名文件（预防422错误）
            delete_existing_asset(target_release, name)
            
            print(f"尝试上传 {name}（尝试 {attempt+1}/{RETRY_COUNT}）")
            uploaded_asset = target_release.upload_asset(
                file_path, name=name, content_type=content_type
            )
            if uploaded_asset:
                return uploaded_asset
            print(f"上传返回 None，重试中...")
        except GithubException as e:
            if e.status == 422:
                print(f"检测到文件冲突，强制删除后重试...")
                delete_existing_asset(target_release, name)
            else:
                print(f"上传失败: {str(e)}，{RETRY_DELAY} 秒后重试")
        except Exception as e:
            print(f"上传失败: {str(e)}，{RETRY_DELAY} 秒后重试")
        time.sleep(RETRY_DELAY)
    print(f"上传 {name} 达到最大重试次数，放弃")
    return None


### 3. 源代码同步（仅判断存在性）
def sync_source_code(tag_name, target_release, synced_data):
    """同步源代码：仅检查目标是否存在文件，不存在则同步"""
    if not target_release:
        print(f"错误：target_release 为 None，无法同步源代码 {tag_name}")
        return False
    
    print(f"\n===== 同步源代码: {tag_name} =====")
    source_files = {
        f"SourceCode_{tag_name}.zip": 
            f"https://github.com/{SOURCE_OWNER}/{SOURCE_REPO_NAME}/archive/refs/tags/{tag_name}.zip",
        f"SourceCode_{tag_name}.tar.gz": 
            f"https://github.com/{SOURCE_OWNER}/{SOURCE_REPO_NAME}/archive/refs/tags/{tag_name}.tar.gz"
    }
    existing_assets = {a.name: a for a in target_release.get_assets()}  # 目标仓库现有文件
    synced_data['source_codes'].setdefault(tag_name, {})
    
    for filename, url in source_files.items():
        # 仅判断目标是否存在该文件
        if filename in existing_assets:
            print(f"目标仓库已存在 {filename}，跳过")
            # 记录存在状态（避免下次重复检查）
            if filename not in synced_data['source_codes'][tag_name]:
                synced_data['source_codes'][tag_name][filename] = {
                    'exists': True,
                    'synced_at': str(datetime.datetime.now())
                }
                save_synced_data(synced_data)
            continue
        
        # 目标不存在，需要同步
        print(f"目标仓库缺失 {filename}，开始同步")
        temp_path = f"temp_{filename}"
        try:
            download_file(url, temp_path)
            uploaded_asset = retry_upload(
                target_release, temp_path, filename, "application/zip"
            )
            
            if uploaded_asset:
                synced_data['source_codes'][tag_name][filename] = {
                    'exists': True,
                    'synced_at': str(datetime.datetime.now())
                }
                save_synced_data(synced_data)
                print(f"同步成功 {filename}")
            else:
                print(f"同步 {filename} 失败")
        except Exception as e:
            print(f"处理 {filename} 失败: {str(e)}")
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
    
    print(f"===== 源代码同步完成: {tag_name} =====")
    return True


### 4. Release附件同步（大小+时间判断）
def sync_release_assets(source_release, target_release, synced_data):
    """同步附件：大小不同 或 源时间更新 则同步"""
    source_id = str(source_release.id)
    source_assets = list(source_release.get_assets())
    target_assets = {a.name: a for a in target_release.get_assets()}
    synced_data['assets'].setdefault(source_id, {})
    
    print(f"\n===== 同步附件（{len(source_assets)} 个）: {source_release.tag_name} =====")
    for asset in source_assets:
        asset_name = asset.name
        asset_key = f"{asset_name}_{asset.size}"  # 临时保留大小用于记录
        content_type = asset.content_type or "application/octet-stream"
        
        # 源文件信息（转为UTC时间）
        source_updated_at = asset.updated_at.astimezone(datetime.timezone.utc) if asset.updated_at else None
        source_info = {
            'size': asset.size,
            'updated_at': source_updated_at.isoformat() if source_updated_at else None
        }
        print(f"源文件 {asset_name} 信息: 大小={source_info['size']}B，时间={source_info['updated_at']}")
        
        # 检查是否需要同步
        need_sync = False
        target_asset = target_assets.get(asset_name)
        target_info = get_asset_info(target_asset)
        
        if asset_key not in synced_data['assets'][source_id]:
            need_sync = True
            print(f"本地记录缺失 {asset_name}，需要同步")
        elif not target_asset:
            need_sync = True
            print(f"目标仓库缺失 {asset_name}，重新同步")
        else:
            # 大小不同则需要同步
            if source_info['size'] != target_info['size']:
                need_sync = True
                print(f"大小不一致: 源={source_info['size']}B 目标={target_info['size']}B")
            # 大小相同但源时间更新则需要同步
            elif source_info['updated_at'] and target_info['updated_at']:
                # 转为datetime对象比较（UTC时间）
                source_time = datetime.datetime.fromisoformat(source_info['updated_at']).timestamp()
                target_time = datetime.datetime.fromisoformat(target_info['updated_at']).timestamp()
                if source_time > target_time:
                    need_sync = True
                    print(f"源文件更新: 源={source_info['updated_at']} 目标={target_info['updated_at']}")
        
        if not need_sync:
            print(f"附件 {asset_name} 无需同步")
            continue
        
        # 下载并上传
        temp_path = f"temp_{asset.id}_{asset_name}"
        try:
            download_file(asset.browser_download_url, temp_path)
            uploaded_asset = retry_upload(
                target_release, temp_path, asset_name, content_type
            )
            
            if uploaded_asset:
                # 记录目标文件信息（用于下次比较）
                actual_info = get_asset_info(uploaded_asset)
                synced_data['assets'][source_id][asset_key] = {
                    'name': asset_name,
                    'size': actual_info['size'],
                    'updated_at': actual_info['updated_at'],
                    'synced_at': str(datetime.datetime.now())
                }
                save_synced_data(synced_data)
                print(f"同步成功 {asset_name}（大小={actual_info['size']}B，时间={actual_info['updated_at']}）")
            else:
                print(f"同步 {asset_name} 失败")
        except Exception as e:
            print(f"处理 {asset_name} 失败: {str(e)}")
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
    
    print(f"===== 附件同步完成: {source_release.tag_name} =====")


### 5. 辅助函数与主函数
def download_file(url, save_path):
    """下载文件（支持断点续传）"""
    if os.path.exists(save_path):
        print(f"文件已存在: {save_path}，跳过下载")
        return save_path
    
    try:
        print(f"开始下载: {url}")
        resp = requests.get(url, stream=True, timeout=600)
        resp.raise_for_status()
        
        with open(save_path, 'wb') as f:
            total_size = int(resp.headers.get('content-length', 0))
            downloaded = 0
            chunk_size = 8192
            
            for chunk in resp.iter_content(chunk_size=chunk_size):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    # 打印进度（每 10MB 更新一次）
                    if downloaded % (10 * 1024 * 1024) < chunk_size and total_size > 0:
                        percent = (downloaded / total_size) * 100
                        print(f"下载进度: {downloaded//(1024*1024):d}MB / {total_size//(1024*1024):d}MB ({percent:.1f}%)")
        
        print(f"下载成功: {save_path}（{os.path.getsize(save_path)} 字节）")
        return save_path
    except Exception as e:
        print(f"下载失败: {str(e)}")
        if os.path.exists(save_path):
            os.remove(save_path)
        raise


def get_or_create_release(target_repo, tag_name, name, body, draft, prerelease):
    """获取或创建Release"""
    release_name = name or tag_name
    print(f"查找 Release: {tag_name}")
    
    # 尝试获取现有Release
    for release in target_repo.get_releases():
        if release.tag_name == tag_name:
            print(f"找到现有 Release: {tag_name}")
            return release
    
    # 创建新Release
    print(f"创建新 Release: {tag_name}")
    try:
        # 确保Tag存在
        try:
            target_repo.get_git_ref(f"tags/{tag_name}")
        except GithubException:
            default_branch = target_repo.default_branch
            print(f"创建 Tag: {tag_name} 基于 {default_branch}")
            target_repo.create_git_ref(
                ref=f"refs/tags/{tag_name}",
                sha=target_repo.get_branch(default_branch).commit.sha
            )
        
        # 创建Release
        release = target_repo.create_git_release(
            tag=tag_name, name=release_name, message=body or "", draft=draft, prerelease=prerelease
        )
        return release
    except Exception as e:
        print(f"创建 Release 失败: {str(e)}")
        # 二次检查是否已存在
        for release in target_repo.get_releases():
            if release.tag_name == tag_name:
                print(f"找到现有 Release（第二轮查找）: {tag_name}")
                return release
        return None


def main():
    synced_data = load_synced_data()
    source_github = Github(SOURCE_GITHUB_TOKEN)
    target_github = Github(GITHUB_TOKEN)
    
    try:
        source_repo = source_github.get_repo(SOURCE_REPO)
        target_repo = target_github.get_repo(TARGET_REPO)
        source_releases = sorted(source_repo.get_releases(), key=lambda r: r.created_at)
        print(f"发现 {len(source_releases)} 个 Release，开始处理...")
        
        for release in source_releases:
            tag_name = release.tag_name
            source_id = str(release.id)
            print(f"\n\n===== 开始处理 Release: {tag_name} =====")
            
            # 获取或创建目标Release
            target_release = get_or_create_release(
                target_repo, tag_name, release.name, release.body, release.draft, release.prerelease
            )
            
            if not target_release:
                print(f"无法获取或创建 {tag_name}，跳过")
                continue
            
            # 同步源代码和附件
            sync_source_code(tag_name, target_release, synced_data)
            sync_release_assets(release, target_release, synced_data)
            
            # 标记为完全同步
            synced_data['releases'][source_id] = {
                'tag_name': tag_name,
                'fully_synced_at': str(datetime.datetime.now())
            }
            save_synced_data(synced_data)
        
        print("\n===== 所有 Release 处理完成 =====")
        print(f"已同步 Release: {len(synced_data['releases'])}")
        print(f"已同步附件: {sum(len(v) for v in synced_data['assets'].values())}")
        print(f"已同步源代码: {sum(len(v) for v in synced_data['source_codes'].values())} 个文件")
    
    except Exception as e:
        print(f"全局错误: {str(e)}")
        traceback.print_exc()
    finally:
        # 清理临时文件
        for f in os.listdir('.'):
            if f.startswith('temp_'):
                os.remove(f)


if __name__ == "__main__":
    main()
