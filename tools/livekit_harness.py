"""Browser harness for the integration's actual LiveKit signaling path.

Listen only on loopback. Refresh tokens stay on the server; no SDP or credentials
are logged. The browser shows receive/decode/render counters and signaling times.
"""
from __future__ import annotations

import argparse
import base64
import asyncio
import json
import logging
from pathlib import Path
from urllib.parse import urlencode
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
logging.getLogger('homeassistant.components.camera.img_util').setLevel(logging.CRITICAL)
from aiohttp import ClientSession, web, WSMsgType
from webrtc_models import RTCIceCandidateInit
from custom_components.simplirtc.livekit import LiveKitSession, LiveKitSignal
from homeassistant.components.camera import WebRTCAnswer
from custom_components.simplirtc.protobufs.livekit_rtc_pb2 import (
    SignalRequest, SessionDescription, TrickleRequest, SignalTarget,
)
from livekit_probe import connection_details
from sdp_bridge_probe import synthesize_answer, split_sdp, mid

HTML = r'''<!doctype html><meta charset="utf-8"><title>SimpliRTC browser harness</title>
<style>body{font:16px system-ui;margin:24px;background:#f6f8fa;color:#17202a}button{font:inherit;padding:10px;margin-right:8px}video{width:480px;max-height:280px;background:#111}pre{white-space:pre-wrap;background:white;padding:16px;border:1px solid #ccc}#result{font-weight:bold}</style>
<h1>SimpliRTC browser harness</h1>
<p>Browser WebRTC experiments. Concurrent warmup uses a separate tab. No microphone or camera capture.</p>
<label>Answer handling <select id="mode"><option value="join-warmed-strict">Real join-with-offer after warmup; forbid renegotiation</option><option value="single-native">Single PC; fresh browser offers on renegotiation</option><option value="empty-join-server">Wrapped join without offer; wait for server offer</option><option value="empty-sdp-server">Join with empty SDP offer; wait for server offer</option><option value="join-dc-sdk">Join /rtc/v1 with offer + reliable/lossy channels</option><option value="join-dc">Join with browser offer + data channel</option><option value="synth">HA offer + data channel; synthesize answers</option><option value="server-offer">LiveKit server offers; Chrome answers</option><option value="ha">HA integration: conditional warmup</option><option value="direct">Pass every answer directly to Chrome</option><option value="reset">Reapply original local offer, then apply every answer</option></select></label><br>
<label>Initial offer delay (ms) <input id="delay" type="number" value="0"></label><br><button id="connect">Start browser test</button><button id="stop">Stop and hide video</button>
<p id="result">Idle</p><video id="video" autoplay muted playsinline controls></video>
<h2>Browser media statistics</h2><pre id="stats">Waiting</pre>
<h2>Signaling and late-track events</h2><pre id="events"></pre>
<script>
const role=new URLSearchParams(location.search).get('role')||'test';
const inlineJoin=()=>mode.startsWith('join-dc')||mode==='join-warmed-strict';
const video=document.querySelector('#video'),output=document.querySelector('#events'),statsEl=document.querySelector('#stats'),result=document.querySelector('#result');
let ws,pc,timer,started,originalOffer,mode,offersSent=0,renegotiationPending=false,extraAudio=0,extraVideo=0,serverOffers=0,answersSent=0,receivedAnswers=0,answerErrors=0,answers=0,rendered=0,pending=[],summary={},last={},first={},ended=false;
const log=(text)=>{output.textContent+=`${((performance.now()-started)/1000).toFixed(2)}s ${text}\n`;};
async function stats(){if(!pc)return;const data=await pc.getStats(),media={};
 for(const r of data.values()){if(r.type!=='inbound-rtp')continue;const kind=r.kind||r.mediaType;if(!['audio','video'].includes(kind))continue;
 const codec=data.get(r.codecId);media[kind]={codec:codec?.mimeType,bytesReceived:r.bytesReceived,packetsReceived:r.packetsReceived,packetsLost:r.packetsLost,jitter:r.jitter};
 for(const key of ['framesDecoded','framesReceived','keyFramesDecoded','frameWidth','frameHeight','framesPerSecond','totalSamplesReceived','concealedSamples','totalAudioEnergy','jitterBufferEmittedCount'])if(r[key]!==undefined)media[kind][key]=r[key];
 const counter=kind==='video'?r.framesDecoded:r.totalSamplesReceived;
 if(counter>0&&!first[kind]){first[kind]=(performance.now()-started)/1000;log(`first decoded ${kind} at ${first[kind].toFixed(2)}s`);}
 if(counter!==last[kind]?.counter)last[kind]={counter,time:performance.now()};
 media[kind].secondsSinceCounterAdvanced=last[kind]?+((performance.now()-last[kind].time)/1000).toFixed(2):null;
 }
 summary={role,browser:navigator.userAgent,elapsedSeconds:+((performance.now()-started)/1000).toFixed(2),connection:pc.connectionState,signaling:pc.signalingState,answerMode:mode,browserOffersSent:offersSent,serverOffersReceived:serverOffers,answersSent,videoPaused:video.paused,answersReceived:receivedAnswers,answerErrors,answersApplied:answers,renderedVideoFrames:rendered,videoCurrentTime:video.currentTime,videoReadyState:video.readyState,firstDecodedSeconds:first,media};
 statsEl.textContent=JSON.stringify(summary,null,2);
 if(ws?.readyState===1)ws.send(JSON.stringify({type:'report',report:summary}));
}
async function flushRenegotiation(){
 if(!renegotiationPending||pc.signalingState!=='stable'||ended)return;
 const audios=extraAudio,videos=extraVideo;renegotiationPending=false;extraAudio=0;extraVideo=0;
 for(let i=0;i<audios;i++)pc.addTransceiver('audio',{direction:'recvonly'});
 for(let i=0;i<videos;i++)pc.addTransceiver('video',{direction:'recvonly'});
 log(`creating fresh offer for renegotiation; added audio=${audios} video=${videos}`);
 const offer=await pc.createOffer();await pc.setLocalDescription(offer);offersSent++;
 ws.send(JSON.stringify({type:'offer',sdp:pc.localDescription.sdp}));log(`sent fresh browser offer ${offersSent}`);
}
function frame(){rendered++;if(!ended)video.requestVideoFrameCallback(frame);}
async function stop(){ended=true;clearInterval(timer);await stats();if(pc)pc.close();if(ws)ws.close();video.srcObject=null;video.hidden=true;result.textContent='Stopped — final counters retained';}
document.querySelector('#stop').onclick=stop;
document.querySelector('#connect').onclick=()=>{if(pc&&pc.connectionState!=='closed')return;
 started=performance.now();mode=document.querySelector('#mode').value;offersSent=0;renegotiationPending=false;extraAudio=0;extraVideo=0;serverOffers=0;answersSent=0;receivedAnswers=0;answerErrors=0;ended=false;answers=0;rendered=0;pending=[];first={};last={};output.textContent='';video.hidden=false;result.textContent='Testing';
 ws=new WebSocket(`ws://${location.host}/signal?mode=${mode}&role=${role}`);let chain=Promise.resolve();
 ws.onmessage=(event)=>{chain=chain.then(async()=>{const m=JSON.parse(event.data);
 if(m.type==='diagnostic'){log(m.message);return;}
 if(m.type==='config'){
 pc=new RTCPeerConnection({iceServers:m.iceServers,bundlePolicy:'max-bundle'});
 if(mode==='synth'||mode==='single-native'||inlineJoin()){for(const label of (['join-dc-sdk','single-native','join-warmed-strict'].includes(mode)?['_reliable','_lossy']:['_reliable'])){const channel=pc.createDataChannel(label,label==='_lossy'?{ordered:true,maxRetransmits:0}:{});channel.onopen=()=>log(`local ${label} data channel open`);log(`created ${label} data channel before offer`);}}
 if(!['server-offer','empty-join-server','empty-sdp-server'].includes(mode)){pc.addTransceiver('audio',{direction:'recvonly'});pc.addTransceiver('video',{direction:'recvonly'});}
 const stream=new MediaStream();video.srcObject=stream;
 pc.ontrack=(e)=>{for(const old of stream.getTracks()){if(old.kind===e.track.kind&&old.id!==e.track.id)stream.removeTrack(old);}stream.addTrack(e.track);log(`browser ontrack ${e.track.kind}`);video.play().catch(error=>log(`play request: ${error.name}`));};
 pc.ondatachannel=(e)=>{log(`server data channel ${e.channel.label}`);e.channel.onopen=()=>log(`data channel open ${e.channel.label}`);};
 video.onpause=()=>log(`video element paused at ${video.currentTime.toFixed(2)}s`);
 video.onplaying=()=>log(`video element playing at ${video.currentTime.toFixed(2)}s`);
 pc.onconnectionstatechange=()=>log(`connection ${pc.connectionState}`);
 pc.onicecandidate=(e)=>{if(e.candidate)ws.send(JSON.stringify({type:'candidate',candidate:e.candidate.toJSON()}));};
 if(!['server-offer','empty-join-server','empty-sdp-server'].includes(mode)){const offer=await pc.createOffer();if(!inlineJoin())await pc.setLocalDescription(offer);originalOffer=inlineJoin()?offer.sdp:pc.localDescription.sdp;log('browser offer ready; waiting before signaling');await new Promise(resolve=>setTimeout(resolve,Number(document.querySelector('#delay').value)));offersSent++;log('sending initial browser offer');log('original offer has application section: '+originalOffer.includes('m=application '));ws.send(JSON.stringify({type:'offer',sdp:originalOffer}));}else{log('waiting for LiveKit server offer');}
 video.requestVideoFrameCallback(frame);timer=setInterval(()=>stats().catch(e=>log(`stats error ${e.name}`)),1000);
 }else if(m.type==='renegotiate'){extraAudio+=m.numAudios;extraVideo+=m.numVideos;renegotiationPending=true;log(`LiveKit requests negotiation audio=${m.numAudios} video=${m.numVideos}`);await flushRenegotiation();
 }else if(m.type==='ice-config'){pc.setConfiguration({iceServers:m.iceServers,bundlePolicy:'max-bundle'});await pc.setLocalDescription({type:'offer',sdp:originalOffer});log('applied original local offer after join supplied ICE configuration');
 }else if(m.type==='offer'){serverOffers++;log(`received server offer ${serverOffers} in ${pc.signalingState}`);await pc.setRemoteDescription({type:'offer',sdp:m.offer});for(const c of pending.splice(0))await pc.addIceCandidate(c);await pc.setLocalDescription(await pc.createAnswer());ws.send(JSON.stringify({type:'answer',sdp:pc.localDescription.sdp}));answersSent++;log(`sent browser answer ${answersSent}`);
 }else if(m.type==='answer'){receivedAnswers++;log(`received answer ${receivedAnswers} in ${pc.signalingState}`);
 if(receivedAnswers>1&&mode==='reset'){log('reapplying ORIGINAL local offer in browser; no new offer sent to LiveKit');await pc.setLocalDescription({type:'offer',sdp:originalOffer});}
 try{await pc.setRemoteDescription({type:'answer',sdp:m.answer});answers++;log(`applied answer ${receivedAnswers}`);}catch(error){answerErrors++;throw error;}
 for(const c of pending.splice(0))await pc.addIceCandidate(c);
 if(mode==='single-native')await flushRenegotiation();
 }else if(m.type==='candidate'){const c=m.candidate?.candidate?m.candidate:null;if(!pc?.remoteDescription)pending.push(c);else await pc.addIceCandidate(c);
 }else if(m.type==='error'){log(`server error ${m.code}`);result.textContent='FAILED';}
 }).catch(e=>{log(`BROWSER ERROR ${e.name}: ${e.message}`);result.textContent='FAILED';});};
 ws.onerror=()=>log('websocket error');
};
</script>'''


class BrowserSession(LiveKitSession):
    def __init__(self, *, diagnostics, forward_replays=False, **kwargs):
        super().__init__(**kwargs)
        self.diagnostics = diagnostics
        self.forward_replays = forward_replays

    async def _on_media_sections_requirement(self, requirement):
        self.diagnostics(f'LiveKit late-track negotiation audio={requirement.num_audios} video={requirement.num_videos}; replay original offer')
        await super()._on_media_sections_requirement(requirement)

    def _on_answer(self, answer):
        forward = bool(self._pending_answers and (self._pending_answers[0] or self.forward_replays))
        self.diagnostics('LiveKit answer: ' + ('forward to browser' if forward else 'consume replay answer internally'))
        sections=answer.sdp.split('m=')[1:]
        self.diagnostics('Answer media layout: '+str([{'kind':m.split()[0], 'ssrc_lines':sum(l.startswith('a=ssrc:') for l in m.splitlines()),'msid_lines':sum(l.startswith('a=msid:') for l in m.splitlines())} for m in sections]))
        if self.forward_replays and self._pending_answers and not self._pending_answers[0]:
            self._pending_answers.popleft()
            if self._send_message:self._send_message(WebRTCAnswer(answer=answer.sdp))
        else:
            super()._on_answer(answer)


class NativeSingleSession(BrowserSession):
    """Forward negotiation requests to the real browser offerer."""
    def __init__(self, *, send_renegotiate, **kwargs):
        super().__init__(**kwargs)
        self.send_renegotiate=send_renegotiate

    async def _read(self):
        class V1Signal(LiveKitSignal):
            @staticmethod
            def _join_url(*args, **kwargs):
                return LiveKitSignal._join_url(*args,**kwargs).replace('/rtc?','/rtc/v1?',1)
        async with V1Signal.connect(self._livekit_url,token=self._user_token,
                auto_subscribe=True,logger=self._logger) as signal:
            async for kind,message in signal:
                if kind=='join':
                    self.diagnostics(f'Single-PC /rtc/v1 join: subscriber_primary={message.subscriber_primary}; initial tracks={sum(len(p.tracks) for p in message.other_participants)}')
                    if message.subscriber_primary:raise RuntimeError('Expected single-PC mode')
                    self._send_ice_servers(list(message.ice_servers))
                    break
            else:raise RuntimeError('No join response')
            async with self._request_queue.sending(signal.send):
                await self._run_browser_signal(signal)

    async def _on_media_sections_requirement(self, requirement):
        self.diagnostics(f'Pass negotiation request to Chrome: audio={requirement.num_audios} video={requirement.num_videos}')
        self.send_renegotiate(requirement.num_audios,requirement.num_videos)


def token_context(token):
    """Compare local participant/room identities without exposing token contents."""
    payload=token.split('.')[1]
    data=json.loads(base64.urlsafe_b64decode(payload+'='*(-len(payload)%4)))
    return {'identity':data.get('sub'),'room':data.get('video',{}).get('room')}


def record_report(request,report):
    state=request.app['state']
    state['last_report']=report
    role=request.query.get('role','test')
    state.setdefault('reports',{})[role]=report
    if role=='warmup':
        state['warmup_report_at']=time.monotonic()
        media=report.get('media',{})
        state['warmup_ready']=(report.get('connection')=='connected'
            and report.get('signaling')=='stable'
            and media.get('video',{}).get('framesDecoded',0)>0
            and media.get('audio',{}).get('totalSamplesReceived',0)>0)


async def server_offer_signaling(request):
    """Relay the legacy subscriber PC: LiveKit offers and Chrome answers."""
    browser_ws = web.WebSocketResponse()
    await browser_ws.prepare(request)
    synth=request.query.get('mode')=='synth'
    single=request.query.get('mode') in ('empty-join-server','empty-sdp-server')
    browser_offer=asyncio.get_running_loop().create_future()
    browser_answer_sent=False
    started = time.monotonic()
    async def diagnostic(message):
        print(f'{time.monotonic()-started:.2f}s {message}', flush=True)
        await browser_ws.send_json({'type':'diagnostic','message':message})
    try:
        details = await connection_details(request.app['args'])
        url = details['liveKitURL'].rstrip('/')+'/rtc?'+urlencode({
            'protocol':16, 'sdk':'js', 'version':'simplirtc-harness',
            'auto_subscribe':'true',
        })
        if single:
            url=LiveKitSignal._join_url(details['liveKitURL'],auto_subscribe=True,offer_sdp='' if request.query.get('mode')=='empty-sdp-server' else None).replace('/rtc?','/rtc/v1?',1)
            await diagnostic('Wrapped /rtc/v1 join; publisher_offer='+('empty SDP' if request.query.get('mode')=='empty-sdp-server' else 'absent')+'; no client offer will be sent')
        async with ClientSession() as http, http.ws_connect(
            url, headers={'Authorization':f"Bearer {details['userToken']}"},
        ) as live_ws, asyncio.TaskGroup() as group:
            signal = LiveKitSignal(ws=live_ws,logger=logging.getLogger('harness.server_offer'),task_group=group)
            async def receive():
                nonlocal browser_answer_sent
                async for kind,message in signal:
                    if kind=='join':
                        await diagnostic(f'Join response: subscriber_primary={message.subscriber_primary}; published tracks={sum(len(p.tracks) for p in message.other_participants)}; server={message.server_info.version}')
                        await browser_ws.send_json({'type':'config','iceServers':[
                            {'urls':list(s.urls),'username':s.username,'credential':s.credential}
                            for s in message.ice_servers
                        ]})
                    elif kind=='offer':
                        layout=[{'kind':m.split()[0], 'ssrc_lines':sum(l.startswith('a=ssrc:') for l in m.splitlines())} for m in message.sdp.split('m=')[1:]]
                        await diagnostic('LiveKit server offer layout: '+str(layout))
                        if not synth:
                            await browser_ws.send_json({'type':'offer','offer':message.sdp})
                        else:
                            local_offer=await browser_offer
                            answer=synthesize_answer(message.sdp,local_offer,setup='active',report=lambda text:print(text,flush=True))
                            await signal.send(SignalRequest(answer=SessionDescription(type='answer',sdp=answer)))
                            await diagnostic('Sent synthesized answer to LiveKit server offer')
                            if not browser_answer_sent:
                                answer=synthesize_answer(local_offer,message.sdp,setup='passive')
                                await browser_ws.send_json({'type':'answer','answer':answer})
                                browser_answer_sent=True
                                await diagnostic('Sent one synthesized answer to HA-style browser offer')
                            else:
                                await diagnostic('Keeping original browser answer; no frontend renegotiation')
                    elif kind=='trickle' and message.target==(SignalTarget.PUBLISHER if single else SignalTarget.SUBSCRIBER):
                        candidate=json.loads(message.candidateInit) if message.candidateInit else None
                        if synth and candidate:
                            original_offer=await browser_offer
                            _,sections=split_sdp(original_offer)
                            candidate['sdpMid']=mid(sections[0])
                            candidate['sdpMLineIndex']=0
                        await browser_ws.send_json({'type':'candidate','candidate':candidate})
                    elif kind=='update':
                        await diagnostic('LiveKit participant update: track counts '+str([len(p.tracks) for p in message.participants]))
                    elif kind=='subscription_permission_update':
                        await diagnostic(f'LiveKit subscription permission allowed={message.allowed}')
                    elif kind=='leave':
                        await diagnostic(f'LiveKit leave reason={message.reason}')
                    elif kind=='media_sections_requirement':
                        await diagnostic(f'Unexpected media-section request {message.num_audios}/{message.num_videos}')
            reader=group.create_task(receive())
            try:
                async for message in browser_ws:
                    if message.type!=WSMsgType.TEXT:continue
                    data=json.loads(message.data)
                    if data['type']=='offer' and synth:
                        browser_offer.set_result(data['sdp'])
                    elif data['type']=='answer':
                        await signal.send(SignalRequest(answer=SessionDescription(type='answer',sdp=data['sdp'])))
                    elif data['type']=='candidate':
                        if synth:
                            data['candidate']['sdpMid']='0'
                            data['candidate']['sdpMLineIndex']=0
                        await signal.send(SignalRequest(trickle=TrickleRequest(
                            target=SignalTarget.PUBLISHER if single else SignalTarget.SUBSCRIBER,candidateInit=json.dumps(data['candidate']),
                        )))
                    elif data['type']=='report':
                        record_report(request,data['report'])
            finally:
                reader.cancel()
                if signal._ping_task:signal._ping_task.cancel()
    except Exception as error:
        print('Server-offer harness error: '+type(error).__name__,flush=True)
        if not browser_ws.closed:
            await browser_ws.send_json({'type':'error','code':type(error).__name__})
    return browser_ws


async def join_offer_data_channel_signaling(request):
    """Include the browser offer in the first/only LiveKit join connection."""
    browser_ws=web.WebSocketResponse()
    await browser_ws.prepare(request)
    started=time.monotonic()
    async def diagnostic(message):
        print(f'{time.monotonic()-started:.2f}s {message}',flush=True)
        await browser_ws.send_json({'type':'diagnostic','message':message})
    try:
        strict=request.query.get('mode')=='join-warmed-strict'
        if strict:
            warm_report=request.app['state'].get('reports',{}).get('warmup',{})
            if not request.app['state'].get('warmup_ready') or time.monotonic()-request.app['state'].get('warmup_report_at',0)>3:
                await browser_ws.send_json({'type':'error','code':'warmup_not_ready'})
                return browser_ws
            await diagnostic('Warmup gating passed: decoded audio and video; warmup connection='+str(warm_report.get('connection')))
        details=await connection_details(request.app['args'])
        if strict:
            context=token_context(details['userToken'])
            warm=request.app['state'].get('warmup_context',{})
            await diagnostic(f'Warmup and real credentials: same participant identity={context["identity"]==warm.get("identity")}; same room={context["room"]==warm.get("room")}')
        await browser_ws.send_json({'type':'config','iceServers':[]})
        original_offer=None
        async for message in browser_ws:
            if message.type!=WSMsgType.TEXT:continue
            data=json.loads(message.data)
            if data['type']=='offer':
                original_offer=data['sdp']
                break
        if original_offer is None:return browser_ws
        await diagnostic('Joining LiveKit with publisher_offer; application section='+str('m=application ' in original_offer))
        class V1JoinSignal(LiveKitSignal):
            @staticmethod
            def _join_url(*args,**kwargs):
                return LiveKitSignal._join_url(*args,**kwargs).replace('/rtc?','/rtc/v1?',1)
        signal_class=V1JoinSignal if request.query.get('mode') in ('join-dc-sdk','join-warmed-strict') else LiveKitSignal
        await diagnostic('Join endpoint: '+('/rtc/v1' if signal_class is V1JoinSignal else '/rtc'))
        async with signal_class.connect(
            details['liveKitURL'],token=details['userToken'],auto_subscribe=True,
            logger=logging.getLogger('harness.join_offer_dc'),offer_sdp=original_offer,
        ) as signal:
            answer_count=0
            async def receive():
                nonlocal answer_count
                async for kind,message in signal:
                    if kind=='join':
                        await diagnostic(f'Offer-in-join response: subscriber_primary={message.subscriber_primary}; published tracks={sum(len(p.tracks) for p in message.other_participants)}; server={message.server_info.version}')
                        await browser_ws.send_json({'type':'ice-config','iceServers':[
                            {'urls':list(s.urls),'username':s.username,'credential':s.credential}
                            for s in message.ice_servers
                        ]})
                    elif kind=='answer':
                        answer_count+=1
                        layout=[{'kind':m.split()[0], 'ssrc_lines':sum(l.startswith('a=ssrc:') for l in m.splitlines())} for m in message.sdp.split('m=')[1:]]
                        await diagnostic(f'LiveKit answer {answer_count}: '+str(layout))
                        if answer_count==1:
                            await browser_ws.send_json({'type':'answer','answer':message.sdp})
                        else:
                            await diagnostic('Consume replay answer internally; browser keeps first answer')
                    elif kind=='trickle' and message.target==SignalTarget.PUBLISHER:
                        candidate=json.loads(message.candidateInit) if message.candidateInit else None
                        await browser_ws.send_json({'type':'candidate','candidate':candidate})
                    elif kind=='media_sections_requirement':
                        if strict:
                            await diagnostic(f'REAL negotiation requested audio={message.num_audios} video={message.num_videos}; no further offer sent')
                            request.app['state'].setdefault('real_negotiations',[]).append({'audio':message.num_audios,'video':message.num_videos})
                        else:
                            await diagnostic(f'Negotiation request audio={message.num_audios} video={message.num_videos}; replay exact original join offer')
                            await signal.send(SignalRequest(offer=SessionDescription(type='offer',sdp=original_offer)))
                    elif kind=='update':
                        await diagnostic('LiveKit participant update: track counts '+str([len(p.tracks) for p in message.participants]))
                    elif kind=='subscription_permission_update':
                        await diagnostic(f'LiveKit subscription permission allowed={message.allowed}')
                    elif kind=='leave':
                        await diagnostic(f'LiveKit leave reason={message.reason}')
            reader=asyncio.create_task(receive())
            try:
                async for message in browser_ws:
                    if message.type!=WSMsgType.TEXT:continue
                    data=json.loads(message.data)
                    if data['type']=='candidate':
                        await signal.send(SignalRequest(trickle=TrickleRequest(
                            target=SignalTarget.PUBLISHER,candidateInit=json.dumps(data['candidate']),
                        )))
                    elif data['type']=='report':record_report(request,data['report'])
            finally:
                reader.cancel()
                await asyncio.gather(reader,return_exceptions=True)
    except Exception as error:
        print('Join-offer data-channel error: '+type(error).__name__,flush=True)
        if not browser_ws.closed:await browser_ws.send_json({'type':'error','code':type(error).__name__})
    return browser_ws


async def signaling(request):
    if request.query.get('mode') in ('join-dc','join-dc-sdk','join-warmed-strict'):
        return await join_offer_data_channel_signaling(request)
    if request.query.get('mode') in ('server-offer','synth','empty-join-server','empty-sdp-server'):
        return await server_offer_signaling(request)
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    queue = asyncio.Queue()
    client = None
    sender = None
    original = LiveKitSignal._response_message
    start = time.monotonic()
    def diagnostic(message):
        queue.put_nowait({'type':'diagnostic','message':message})
        print(f'{time.monotonic()-start:.2f}s {message}', flush=True)
    def observed(signal, response):
        kind = response.WhichOneof('message')
        if kind == 'join':
            diagnostic(f'LiveKit join: {sum(len(p.tracks) for p in response.join.other_participants)} published tracks')
        elif kind == 'leave':
            diagnostic(f'LiveKit leave reason={response.leave.reason}')
        elif kind == 'update':
            diagnostic('LiveKit participant update: track counts ' + str([len(p.tracks) for p in response.update.participants]))
        return original(signal, response)
    try:
        details = await connection_details(request.app['args'])
        if request.query.get('role')=='warmup':
            request.app['state']['warmup_context']=token_context(details['userToken'])
        LiveKitSignal._response_message = observed
        ready = asyncio.get_running_loop().create_future()
        def ice_servers(servers):
            ready.set_result([{'urls':list(s.urls),'username':s.username,'credential':s.credential} for s in servers])
        def closed():
            if request.query.get('role')=='warmup':request.app['state']['warmup_ready']=False
            if not ready.done():ready.set_exception(RuntimeError('Signaling closed'))
        client_class=NativeSingleSession if request.query.get('mode')=='single-native' else BrowserSession
        extra={}
        if client_class is NativeSingleSession:
            extra['send_renegotiate']=lambda audio,video:queue.put_nowait({'type':'renegotiate','numAudios':audio,'numVideos':video})
        client = client_class(**extra,livekit_url=details['liveKitURL'], user_token=details['userToken'],send_ice_servers=ice_servers,diagnostics=diagnostic,forward_replays=request.query.get('mode') in ('direct','reset'))
        client.start(closed)
        servers = await asyncio.wait_for(ready, 15)
        await ws.send_json({'type':'config','iceServers':servers})
        async def send():
            while True:await ws.send_json(await queue.get())
        sender = asyncio.create_task(send())
        async for message in ws:
            if message.type != WSMsgType.TEXT:continue
            data = json.loads(message.data)
            if data['type']=='offer':await client.handle_offer(data['sdp'],lambda m:queue.put_nowait(m.as_dict()))
            elif data['type']=='candidate':await client.send_candidate(RTCIceCandidateInit.from_dict(data['candidate']))
            elif data['type']=='report':record_report(request,data['report'])
    except Exception as error:
        diagnostic('Harness error: '+type(error).__name__)
    finally:
        LiveKitSignal._response_message = original
        if client:
            client.close()
            await client.wait_closed()
        if sender:
            sender.cancel()
            await asyncio.gather(sender,return_exceptions=True)
    return ws


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    credentials=parser.add_mutually_exclusive_group(required=True)
    credentials.add_argument('--refresh-token-file',type=Path)
    credentials.add_argument('--details-file',type=Path)
    parser.add_argument('--camera',help='Camera name (default: first LiveKit camera)')
    parser.add_argument('--port',type=int,default=8765)
    args=parser.parse_args()
    logging.disable(logging.CRITICAL)
    app=web.Application()
    app['args']=args
    app['state']={}
    async def index(_):return web.Response(text=HTML,content_type='text/html')
    async def report(request):
        if request.query.get('all')=='1':
            return web.json_response({key:app['state'].get(key) for key in ('reports','warmup_ready','real_negotiations')})
        return web.json_response(app['state'].get('last_report',{}))
    app.router.add_get('/',index)
    app.router.add_get('/signal',signaling)
    app.router.add_get('/report',report)
    web.run_app(app,host='127.0.0.1',port=args.port,access_log=None)


if __name__=='__main__':main()
