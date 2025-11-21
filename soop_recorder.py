import argparse
import asyncio
import json
import logging
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# curl_cffi: 브라우저 위장을 위한 필수 라이브러리
from curl_cffi.requests import AsyncSession

# --- 설정 파일 이름 ---
CONFIG_FILE = "settings.json"

# --- 로깅 설정 ---
log = logging.getLogger("soop_recorder")
log.setLevel(logging.DEBUG)
if not log.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[%(asctime)s][%(levelname)s] %(message)s", "%H:%M:%S"))
    log.addHandler(handler)

# --- API 주소 ---
STATION_URL = "https://chapi.sooplive.co.kr/api/{streamer_id}/station"
PLAYER_LIVE_API_URL = "http://live.sooplive.co.kr/afreeca/player_live_api.php"
VIEW_URL = "https://livestream-manager.sooplive.co.kr/broad_stream_assign.html"

def clean_filename(filename):
    return re.sub(r'[\\/*?:\"<>|]', "", filename)

def load_config():
    """settings.json 파일을 읽어옵니다."""
    config_path = Path(CONFIG_FILE)
    if config_path.exists():
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                log.info(f"설정 파일({CONFIG_FILE})을 로드했습니다.")
                return json.load(f)
        except Exception as e:
            log.error(f"설정 파일 로드 실패: {e}")
            return {}
    return {}

class SoopRecorder:
    def __init__(self, streamer_id, output_dir=".", proxy=None, poll_interval=15):
        self.streamer_id = streamer_id
        self.output_dir = Path(output_dir)
        self.proxy_str = proxy
        self.poll_interval = poll_interval
        self.session = None
        self.streamer_name = self.streamer_id

        self.output_dir.mkdir(parents=True, exist_ok=True)
        log.info(f"녹화 파일 저장 경로: {self.output_dir.resolve()}")

    async def __aenter__(self):
        # 인증용 세션에만 프록시를 적용 (AID 획득용)
        proxies = {"http": self.proxy_str, "https": self.proxy_str} if self.proxy_str else None
        
        self.session = AsyncSession(
            impersonate="chrome110",  # 브라우저 위장 (그리드 우회 핵심)
            proxies=proxies,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36"}
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    async def _get_aid_token(self, broad_no):
        """프록시를 이용해 원본 화질 AID 토큰을 요청합니다."""
        data = { 
            "bid": self.streamer_id, 
            "mode": "landing", 
            "player_type": "html5", 
            "quality": "original", 
            "type": "aid" 
        }
        try:
            log.debug(f"AID 토큰 요청 (프록시: {'사용' if self.proxy_str else '미사용'})")
            response = await self.session.post(PLAYER_LIVE_API_URL, data=data, timeout=15)
            response.raise_for_status()
            
            try:
                res_json = json.loads(response.text)
            except json.JSONDecodeError:
                log.error("서버 응답이 JSON 형식이 아닙니다.")
                return None

            if res_json.get("CHANNEL", {}).get("RESULT") == 1:
                return res_json["CHANNEL"]["AID"]
            else:
                log.warning(f"AID 토큰 요청 실패 (응답코드: {res_json.get('CHANNEL', {}).get('RESULT')})")
                return None
        except Exception as e:
            log.error(f"AID 토큰 요청 중 오류 발생: {e}")
            return None

    async def check_stream_status(self):
        """방송 상태 확인 및 스트림 정보 획득"""
        try:
            # 방송국 정보는 프록시 없이 직접 호출 (속도 향상)
            async with AsyncSession(impersonate="chrome110") as temp_session:
                response = await temp_session.get(STATION_URL.format(streamer_id=self.streamer_id), timeout=10)
                response.raise_for_status()
                data = response.json()
        except Exception as e:
            log.error(f"방송국 정보 조회 실패: {e}")
            return None

        broad_info = data.get("broad")
        self.streamer_name = data.get("station", {}).get("user_nick", self.streamer_id)

        if not broad_info:
            return None

        broad_no = broad_info["broad_no"]
        title = broad_info["broad_title"]
        log.info(f"방송 감지됨: {title}")

        # 1. AID 토큰 획득 (여기서만 프록시 사용)
        aid = await self._get_aid_token(broad_no)
        if not aid:
            log.error("원본 화질 AID 토큰을 얻지 못했습니다. (WireGuard/프록시 상태를 확인하세요)")
            return None
        
        try:
            # 2. View URL 획득 (프록시 불필요)
            params = { "return_type": "gcp_cdn", "broad_key": f"{broad_no}-common-original-hls" }
            async with AsyncSession(impersonate="chrome110") as temp_session:
                res_view = await temp_session.get(VIEW_URL, params=params, timeout=10)
                res_view.raise_for_status()
                view_data = res_view.json()

            if view_data.get("view_url"):
                m3u8_url = f"{view_data['view_url']}?aid={aid}"
                return {"m3u8_url": m3u8_url, "title": title}
            else:
                log.error("스트림 주소(view_url)를 가져오지 못했습니다.")
                return None
        except Exception as e:
            log.error(f"스트림 주소 요청 중 오류 발생: {e}")
            return None

    async def record_stream(self, stream_info):
        """Streamlink를 사용하여 녹화 (오디오/비디오 동기화 해결)"""
        m3u8_url = stream_info["m3u8_url"]
        title = clean_filename(stream_info["title"])
        streamer_name = clean_filename(self.streamer_name)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_filename = self.output_dir / f"[{streamer_name}]_{timestamp}_{title}.ts"

        log.info(f"녹화를 시작합니다: {output_filename.name}")

        # 헤더 설정
        referer = f"https://play.sooplive.co.kr/{self.streamer_id}"
        user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36"

        # streamlink 명령어 구성 (프록시 없음 -> 한국 IP 직통 다운로드)
        streamlink_cmd = [
            "streamlink",
            "--http-header", f"User-Agent={user_agent}",
            "--http-header", f"Referer={referer}",
            "--force",
            m3u8_url,
            "best",
            "-o", str(output_filename)
        ]

        try:
            process = await asyncio.create_subprocess_exec(
                *streamlink_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            log.info("방송이 종료되거나 Ctrl+C를 누를 때까지 녹화합니다...")
            
            # 녹화 진행
            await process.communicate()
            
            if process.returncode == 0:
                log.info("녹화가 정상적으로 완료되었습니다.")
            else:
                log.warning(f"녹화가 종료되었습니다 (종료 코드: {process.returncode}).")

        except FileNotFoundError:
            log.error("streamlink가 설치되어 있지 않습니다. (pip install streamlink)")
            sys.exit(1)
        except Exception as e:
            log.error(f"녹화 중 예외 발생: {e}")

    async def run(self):
        log.info(f"'{self.streamer_name}' ({self.streamer_id}) 방송 감시 시작... (주기: {self.poll_interval}초)")
        while True:
            try:
                stream_info = await self.check_stream_status()
                if stream_info:
                    await self.record_stream(stream_info)
                    log.info("녹화 종료. 1분 대기.")
                    await asyncio.sleep(60)
                else:
                    await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"오류: {e}")
                await asyncio.sleep(self.poll_interval)

async def main():
    # 1. 설정 파일 로드
    config = load_config()

    # 2. 명령행 인자 파싱
    parser = argparse.ArgumentParser(description="SOOP 1080p 녹화기")
    parser.add_argument("streamer_id", nargs='?', help="녹화할 스트리머 ID")
    parser.add_argument("-o", "--output-dir", help="저장 폴더")
    parser.add_argument("-i", "--poll-interval", type=int, help="확인 주기(초)")
    parser.add_argument("--wg-conf", help="WireGuard 설정 파일 경로")
    parser.add_argument("--wireproxy-path", help="wireproxy 실행 파일 경로")
    parser.add_argument("-p", "--proxy", help="수동 프록시 주소")
    
    args = parser.parse_args()

    # 3. 설정 값 병합
    streamer_id = args.streamer_id or config.get("streamer_id")
    output_dir = args.output_dir or config.get("output_dir", ".")
    poll_interval = args.poll_interval or config.get("poll_interval", 15)
    wg_conf = args.wg_conf or config.get("wg_conf")
    wireproxy_path = args.wireproxy_path or config.get("wireproxy_path", "wireproxy") # PATH에 있으면 그냥 wireproxy
    active_proxy = args.proxy or config.get("proxy")

    if not streamer_id:
        log.error("스트리머 ID가 없습니다. (명령행 인자 또는 settings.json 확인)")
        sys.exit(1)

    # streamlink 체크
    try:
        subprocess.run(["streamlink", "--version"], capture_output=True, check=True)
    except:
        log.error("streamlink가 설치되어 있지 않습니다.")
        sys.exit(1)

    wg_process = None

    # WireGuard 자동 실행
    if wg_conf:
        log.info(f"WireGuard 프록시 시작 중... (설정: {wg_conf})")
        try:
            # 에러 확인을 위해 stdout/stderr DEVNULL 제거
            wg_process = subprocess.Popen(
                [wireproxy_path, "-c", wg_conf]
            )
            time.sleep(2)
            
            if wg_process.poll() is not None:
                log.error("wireproxy 실행 실패. 설정 파일에 [Socks5] 섹션이 있는지 확인하세요.")
                sys.exit(1)
            
            log.info("WireGuard 프록시 활성화됨 (127.0.0.1:1080)")
            active_proxy = "socks5://127.0.0.1:1080"
            
        except FileNotFoundError:
            log.error(f"'{wireproxy_path}' 파일을 찾을 수 없습니다.")
            sys.exit(1)

    try:
        async with SoopRecorder(
            streamer_id=streamer_id,
            output_dir=output_dir,
            proxy=active_proxy,
            poll_interval=poll_interval
        ) as recorder:
            await recorder.run()
            
    except KeyboardInterrupt:
        log.info("종료 중...")
    
    finally:
        if wg_process:
            log.info("WireGuard 종료.")
            wg_process.terminate()
            try:
                wg_process.wait(timeout=3)
            except:
                wg_process.kill()

if __name__ == "__main__":
    asyncio.run(main())