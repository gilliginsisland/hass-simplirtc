"""Signaling-only LiveKit/WHEP facade plus a browser/go2rtc signaling proxy.

Python opens HTTP and LiveKit signaling websockets only. It never creates a
WebRTC peer or receives/forwards media. Use an isolated loopback go2rtc instance.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
import re
import time
from uuid import uuid4

from aiohttp import ClientSession, WSMsgType, web
from livekit_harness import HTML
from livekit_probe import connection_details
from custom_components.simplirtc.livekit import LiveKitSignal
from custom_components.simplirtc.protobufs.livekit_rtc_pb2 import (
    SessionDescription, SignalRequest, SignalTarget,
)


class V1Signal(LiveKitSignal):
    @staticmethod
    def _join_url(*args, **kwargs):
        return LiveKitSignal._join_url(*args, **kwargs).replace('/rtc?', '/rtc/v1?', 1)


def with_candidates(sdp, candidates):
    parts=sdp.replace('\r\n','\n').strip().split('\nm=')
    media=[('m='+part).splitlines() for part in parts[1:]]
    for candidate in candidates:
        line='a='+candidate['candidate']
        for index,section in enumerate(media):
            mids=[row[6:] for row in section if row.startswith('a=mid:')]
            if candidate.get('sdpMid') in mids or candidate.get('sdpMLineIndex')==index:
                if line not in section:section.append(line)
                break
    return '\r\n'.join(parts[0].splitlines()+[line for section in media for line in section])+'\r\n'


class WhepSession:
    def __init__(self,app,offer):
        self.app=app
        self.offer=offer
        self.id=uuid4().hex
        self.answer=asyncio.get_running_loop().create_future()
        self.ice_complete=asyncio.Event()
        self.candidates=[]
        self.answer_count=0
        self.task=asyncio.create_task(self.run())

    def log(self,message):
        self.app['state']['events'].append(message)
        print(message,flush=True)
        for ws in tuple(self.app['state']['browsers']):
            if not ws.closed:asyncio.create_task(ws.send_json({'type':'diagnostic','message':message}))

    async def run(self):
        try:
            details=await connection_details(self.app['args'])
            self.log('go2rtc offer ICE candidate protocols: '+str(sorted({line.split()[2] for line in self.offer.splitlines() if line.startswith('a=candidate:')})))
            self.log(f'Cold WHEP source: forwarding go2rtc offer unchanged; application section={"m=application " in self.offer}')
            async with V1Signal.connect(details['liveKitURL'],token=details['userToken'],
                    auto_subscribe=True,logger=logging.getLogger('whep'),offer_sdp=self.offer) as signal:
                async for kind,message in signal:
                    if kind=='join':
                        self.log(f'LiveKit join: server={message.server_info.version}; subscriber_primary={message.subscriber_primary}; initial published tracks={sum(len(p.tracks) for p in message.other_participants)}')
                    elif kind=='answer':
                        self.answer_count+=1
                        counts=[{'kind':m.split()[0],'ssrc_lines':sum(line.startswith('a=ssrc:') for line in m.splitlines())} for m in message.sdp.split('m=')[1:]]
                        self.log(f'LiveKit answer {self.answer_count}: {counts}')
                        if not self.answer.done():self.answer.set_result(message.sdp)
                        else:self.log('Replay answer consumed internally; WHEP client keeps initial answer')
                    elif kind=='trickle' and message.target==SignalTarget.PUBLISHER:
                        if message.candidateInit:
                            candidate=json.loads(message.candidateInit)
                            if candidate.get('candidate'):self.candidates.append(candidate)
                            else:self.ice_complete.set()
                        if message.final:self.ice_complete.set()
                    elif kind=='media_sections_requirement':
                        self.log(f'LiveKit negotiation request audio={message.num_audios} video={message.num_videos}; replay exact go2rtc offer')
                        await signal.send(SignalRequest(offer=SessionDescription(type='offer',sdp=self.offer)))
                    elif kind=='update':
                        self.log('Participant track counts '+str([len(p.tracks) for p in message.participants]))
                    elif kind=='subscription_permission_update':
                        self.log(f'Subscription permission allowed={message.allowed}')
                    elif kind=='leave':
                        self.log(f'LiveKit leave reason={message.reason}')
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.log('LiveKit signaling error: '+type(error).__name__)
            if not self.answer.done():self.answer.set_exception(RuntimeError(type(error).__name__))

    async def close(self):
        self.task.cancel()
        await asyncio.gather(self.task,return_exceptions=True)


async def whep(request):
    session=WhepSession(request.app,await request.text())
    request.app['state']['sessions'][session.id]=session
    try:
        answer=await asyncio.wait_for(asyncio.shield(session.answer),15)
        try:await asyncio.wait_for(session.ice_complete.wait(),2)
        except TimeoutError:pass
        answer=with_candidates(answer,session.candidates)
        session.log(f'Return initial WHEP answer; ICE candidate lines={sum(line.startswith("a=candidate:") for line in answer.splitlines())}')
        return web.Response(status=201,text=answer,content_type='application/sdp',headers={'Location':'/whep/'+session.id})
    except Exception as error:
        await session.close()
        raise web.HTTPBadGateway(text=type(error).__name__) from None


async def remove(request):
    if session:=request.app['state']['sessions'].pop(request.match_info['id'],None):await session.close()
    return web.Response(status=204)


async def browser_signaling(request):
    browser_ws=web.WebSocketResponse()
    await browser_ws.prepare(request)
    state=request.app['state']
    state['browsers'].add(browser_ws)
    try:
        async with ClientSession() as http, http.ws_connect(request.app['args'].go2rtc_url+'/api/ws?src=livekit') as go_ws:
            await browser_ws.send_json({'type':'config','iceServers':[]})
            async def receive():
                async for message in go_ws:
                    if message.type!=WSMsgType.TEXT:continue
                    data=json.loads(message.data)
                    if data['type']=='webrtc':
                        value=data['value']
                        await browser_ws.send_json({'type':'answer','answer':value['sdp']})
                    elif data['type']=='webrtc/answer':
                        await browser_ws.send_json({'type':'answer','answer':data['value']})
                    elif data['type']=='webrtc/candidate':
                        await browser_ws.send_json({'type':'candidate','candidate':{'candidate':data['value'],'sdpMid':'0','sdpMLineIndex':0}})
                    elif data['type']=='error':
                        # Error text may contain upstream addresses; retain a type only.
                        await browser_ws.send_json({'type':'error','code':'go2rtc_error'})
                        state['events'].append('go2rtc reported an error')
            reader=asyncio.create_task(receive())
            try:
                async for message in browser_ws:
                    if message.type!=WSMsgType.TEXT:continue
                    data=json.loads(message.data)
                    if data['type']=='offer':
                        await go_ws.send_json({'type':'webrtc','value':{'type':'offer','sdp':data['sdp'],'ice_servers':[]}})
                    elif data['type']=='candidate':
                        await go_ws.send_json({'type':'webrtc/candidate','value':data['candidate']['candidate']})
                    elif data['type']=='report':state['report']=data['report']
            finally:
                reader.cancel()
                await asyncio.gather(reader,return_exceptions=True)
    except Exception as error:
        print('Browser signaling proxy error: '+type(error).__name__,flush=True)
    finally:state['browsers'].discard(browser_ws)
    return browser_ws


async def index(_):
    html=re.sub(r'<select id="mode">.*?</select>','<select id="mode"><option value="go2rtc-whep">go2rtc consumes signaling-only LiveKit WHEP facade</option></select>',HTML,flags=re.S)
    html=html.replace('Browser WebRTC experiments. Concurrent warmup uses a separate tab. No microphone or camera capture.',
        'Cold LiveKit → go2rtc → Chrome. Python handles signaling only. No warmup, media relay, or transcoding in Python.')
    return web.Response(text=html,content_type='text/html')


async def report(request):
    state=request.app['state']
    return web.json_response({'browser':state.get('report'),'events':state['events']})


async def shutdown(app):
    for session in app['state']['sessions'].values():await session.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--refresh-token-file',type=Path,required=True)
    parser.add_argument('--camera',help='Camera name (default: first LiveKit camera)')
    parser.add_argument('--go2rtc-url',default='http://127.0.0.1:11985')
    parser.add_argument('--port',type=int,default=8767)
    args=parser.parse_args()
    args.details_file=None
    logging.disable(logging.CRITICAL)
    app=web.Application()
    app['args']=args
    app['state']={'sessions':{},'events':[],'browsers':set()}
    app.router.add_get('/',index)
    app.router.add_get('/signal',browser_signaling)
    app.router.add_post('/whep',whep)
    app.router.add_delete('/whep/{id}',remove)
    app.router.add_get('/report',report)
    app.on_shutdown.append(shutdown)
    web.run_app(app,host='127.0.0.1',port=args.port,access_log=None)


if __name__=='__main__':main()
