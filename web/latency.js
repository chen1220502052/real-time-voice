/* Explicit clock domains. Network bounds require a ping calibration; no RTT/2
   assertion, physical speaker timestamp, or polling timestamp is fabricated. */
(function () {
  class LatencyTrace {
    constructor() { this.reset(); }
    reset() {
      this.replies = new Map(); this.captures = []; this.pings = new Map();
      this.calibrations = []; this.orphanFrames = 0;
    }
    reply(id) {
      if (!this.replies.has(id)) this.replies.set(id, {replyId:id, server:{}, frames:0,
        physicalOutput:null, physicalOutputStatus:"not_measured"});
      if (this.replies.size > 128) this.replies.delete(this.replies.keys().next().value);
      return this.replies.get(id);
    }
    ping(id, now) { this.pings.set(id, now); if(this.pings.size>16)this.pings.delete(this.pings.keys().next().value); }
    pong(message, now) {
      const sent = this.pings.get(message.pingId); this.pings.delete(message.pingId);
      if (sent === undefined || !Number.isFinite(message.serverReceivedMs) || !Number.isFinite(message.serverSentMs)) return;
      const low = message.serverSentMs-now, high = message.serverReceivedMs-sent;
      if (high < low) return;
      this.calibrations.push({offsetLowMs:low, offsetHighMs:high,
        uncertaintyWidthMs:high-low, roundTripMs:now-sent, sampledClientMs:now});
      if(this.calibrations.length>32)this.calibrations.shift();
    }
    capture(frame) { const {pcm,...metadata}=frame;this.captures.push(metadata); if(this.captures.length>12000)this.captures.shift(); }
    receive(frame, now, ctx) {
      if (!frame.replyId) { this.orphanFrames++; return; }
      const r=this.reply(frame.replyId); r.turnId=frame.turnId; r.generationEpoch=frame.generationEpoch;
      Object.assign(r.server, frame.timing || {});
      if (r.firstPacketReceivedMs === undefined) {
        r.firstPacketReceivedMs=now; r.firstPacketReceivedCtx=ctx;
      }
      r.frames++;
      if(r.frames===3)r.thirdPacketReceivedMs=now;
    }
    serverEvent(event, receivedMs) {
      if(!event.replyId)return;
      const r=this.reply(event.replyId);
      if(event.attributes?.timing)Object.assign(r.server,event.attributes.timing);
      if(event.eventType==="playback_stream_started")r.sendEventObservedMs=receivedMs;
    }
    schedule(frame, startCtx, now) {
      const r=this.reply(frame.replyId);
      if(r.firstScheduledCtx===undefined) { r.firstScheduledCtx=startCtx; r.scheduleCalledMs=now; }
      const first=frame.pcm.findIndex(sample=>Math.abs(sample)>=300);
      if(first>=0 && r.firstSignalScheduledCtx===undefined)r.firstSignalScheduledCtx=startCtx+first/16000;
    }
    graph(message, audioContext, receivedMs) {
      const r=this.reply(message.replyId);
      if(r.graphOnsetCtx!==undefined)return;
      r.graphOnsetCtx=message.onsetCtx; r.graphEventObservedMs=receivedMs;
      r.baseLatencySeconds=audioContext.baseLatency ?? null;
      r.outputLatencySeconds=audioContext.outputLatency ?? null;
      let stamp;
      try { stamp=audioContext.getOutputTimestamp?.(); } catch (_) { /* Closed/unsupported device: unmeasured. */ }
      if(stamp && stamp.performanceTime>0 && Number.isFinite(stamp.contextTime)) {
        r.deviceOutputEstimateMs=stamp.performanceTime+(message.onsetCtx-stamp.contextTime)*1000;
        r.deviceOutputEstimateStatus="browser_estimate_not_physical_measurement";
      }
    }
    stop(replyId, ctx) {
      const r=this.replies.get(replyId); if(!r)return;
      r.stopCtx=ctx;
      if(r.firstSignalScheduledCtx>=ctx)r.scheduledSignalCancelled=true;
    }
    networkBounds(serverMs, clientMs, direction, calibration) {
      if(!calibration||!Number.isFinite(serverMs)||!Number.isFinite(clientMs))return null;
      const low=direction==="up" ? serverMs-clientMs-calibration.offsetHighMs : clientMs+calibration.offsetLowMs-serverMs;
      const high=direction==="up" ? serverMs-clientMs-calibration.offsetLowMs : clientMs+calibration.offsetHighMs-serverMs;
      return {lowerMs:low, upperMs:high, clockUncertaintyWidthMs:calibration.uncertaintyWidthMs,
        status:high<0?"inconsistent_clock_sample":"bounded_not_exact_one_way"};
    }
    report() {
      const calibration=this.calibrations.reduce((best,item)=>!best||item.uncertaintyWidthMs<best.uncertaintyWidthMs?item:best,null);
      const difference=(a,b)=>Number.isFinite(a)&&Number.isFinite(b)?a-b:null;
      const secondsDifference=(a,b)=>{const value=difference(a,b);return value===null?null:value*1000;};
      const rows=[];
      for(const r of this.replies.values()) {
        const s=r.server, local=s.localAsr||{};
        const candidates=this.captures.filter(frame=>Number.isInteger(frame.sequence)&&
          frame.sequence>=Math.floor((s.speechStartMediaMs??0)/100)&&frame.sequence<=s.processedSequence);
        const end=candidates.filter(frame=>Number.isFinite(frame.lastSignalCtx)).at(-1);
        const receipts=new Map(s.inputReceipts||[]);
        const received=end?receipts.get(end.sequence):undefined;
        const serverParts={
          sourceEndFrameToFinalMs:difference(s.asrFinalMs,received),
          finalFrameQueueMs:difference(s.finalProcessingStartedMs,s.finalInputReceivedMs),
          finalFrameAsrProcessingMs:difference(s.asrFinalMs,s.finalProcessingStartedMs),
          asrRequestWallMs:difference(local.responseReadyMs,local.requestStartedMs),
          asrWorkerInferenceMs:local.workerInferenceMs??null,
          asrWorkerQueueWaitMs:local.workerQueueWaitMs??null,
          asrWorkerStartupMs:local.workerStartupMs??null,
          asrWorkerIpcWallMs:local.workerIpcWallMs??null,
          asrOverlappedEndpointMs:Number.isFinite(local.responseReadyMs)&&Number.isFinite(local.endpointReachedMs)&&Number.isFinite(local.requestStartedMs)?
            Math.max(0,Math.min(local.responseReadyMs,local.endpointReachedMs)-local.requestStartedMs):null,
          asrWaitAfterEndpointMs:Number.isFinite(local.responseReadyMs)&&Number.isFinite(local.endpointReachedMs)?
            Math.max(0,local.responseReadyMs-local.endpointReachedMs):null,
          finalToBotRequestMs:difference(s.botRequestStartedMs,s.asrFinalMs),
          botToFirstTextMs:difference(s.botFirstTextMs,s.botRequestStartedMs),
          firstTextToTtsMs:difference(s.ttsStartedMs,s.botFirstTextMs),
          ttsToFirstPcmMs:difference(s.ttsFirstPcmMs,s.ttsStartedMs),
          pcmToFrameMs:difference(s.firstPcmEnqueuedMs,s.ttsFirstPcmMs),
          frameQueueMs:difference(s.firstFrameSendQueuedMs,s.firstPcmEnqueuedMs),
          sendLockWaitMs:difference(s.firstFrameSendStartedMs,s.firstFrameSendQueuedMs),
          sendAwaitMs:difference(s.firstFrameSendCompletedMs,s.firstFrameSendStartedMs),
        };
        rows.push({...r, sourceSignalEndCtx:end?.lastSignalCtx??null, sourceSignalEndSequence:end?.sequence??null,
          clockDomain:"explicit_per_field", serverParts,
          clientParts:{
            signalToPacketEndMs:end?secondsDifference(end.captureEndCtx,end.lastSignalCtx):null,
            captureDeliveryLagMs:end?secondsDifference(end.deliveredCtx,end.captureEndCtx):null,
            captureCallbackToSendMs:end?difference(end.sentClientMs,end.deliveredClientMs):null,
            firstToThirdPacketMs:difference(r.thirdPacketReceivedMs,r.firstPacketReceivedMs),
            receiveToScheduleCallMs:difference(r.scheduleCalledMs,r.firstPacketReceivedMs),
            receiveToPlannedSignalMs:secondsDifference(r.firstSignalScheduledCtx,r.firstPacketReceivedCtx),
            plannedToGraphSignalMs:secondsDifference(r.graphOnsetCtx,r.firstSignalScheduledCtx),
            captureSignalToGraphMs:end&&Number.isFinite(r.graphOnsetCtx)?(r.graphOnsetCtx-end.lastSignalCtx)*1000:null,
          },
          // Offset cancels here. This is aggregate uplink+downlink transport
          // and JS delivery delay, never a claimed exact one-way measurement.
          combinedTransportMs:end&&Number.isFinite(received)&&Number.isFinite(s.firstFrameSendStartedMs)&&
            Number.isFinite(end.sentClientMs)?r.firstPacketReceivedMs-end.sentClientMs-
              (s.firstFrameSendStartedMs-received):null,
          uplinkBounds:end?this.networkBounds(received,end.sentClientMs,"up",calibration):null,
          downlinkBounds:this.networkBounds(s.firstFrameSendStartedMs,r.firstPacketReceivedMs,"down",calibration),
          calibrationAgeMs:calibration?difference(r.firstPacketReceivedMs,calibration.sampledClientMs):null,
        });
      }
      return {schemaVersion:2, calibration, orphanFrames:this.orphanFrames, replies:rows,
        notes:["PCM receipt, planned signal, graph rendering and device estimate are different milestones.",
          "physicalOutput stays null without an independently calibrated physical/loopback recording.",
          "Source signal end uses abs(PCM16)>=300; it is not a linguistic speech-end detector.",
          "Server parts include overlapping diagnostics; do not sum every field."]};
    }
  }
  window.LatencyTrace=LatencyTrace;
})();
