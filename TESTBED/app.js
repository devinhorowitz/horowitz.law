/* Sidebar — QPWB drafting workspace (prototype). CSP-clean: no inline handlers, external script. */
"use strict";

const check='<svg viewBox="0 0 12 12"><path d="M2 6l3 3 5-6" fill="none" stroke="#fff" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const JL={generic:'Generic','ga-state':'GA State','ga-fed':'N.D. Ga. (Fed)','fl-state':'FL State','fl-fed':'S.D. Fla. (Fed)','tn-state':'TN State'};
function jclass(j){return j==='generic'?'generic':(j.endsWith('-fed')||j==='fed')?'fed':'state';}
const AUTHW={'Case':1800,'Statute':400,"Shepard's":350,'Memo':900};

/* ---- BASE ---- */
const base={id:'base',title:"Base — drafting standard",ver:"v6 · 2026-05-09",scope:'generic',
 body:`Word choice: "plan to" not "intend to"; "under" not "pursuant to"; "promptly" not "immediately"; "harm" not "prejudice"; "need" not "require".
Em dashes: briefs and motions only; none in letters, emails, or chat.
Anti-hallucination: never invent names, emails, citations. Flag gaps; ask when uncertain.
Pleadings: bracket caption; two-column signature table. The /s/ line and typed name match the signing attorney.
Output: concise, direct, no preamble, no recap.`};

/* ---- LIBRARY (scope-tagged) ---- */
let library=[
 {id:'word',grp:'Voice',title:'Word choice & register',ver:'v4 · 2026-05-09',scope:'generic',prov:null,
  body:'Natural register for letters and memos; formal for briefs and motions. Oxford commas. "about" not "approximately"; active voice.'},
 {id:'depoObj',grp:'Depositions',title:'Deposition objection bank',ver:'v2 · 2026-05-05',scope:'generic',prov:null,
  body:'FORM — preserve at the moment or waived. LEADING — direct of own witness. FOUNDATION — no personal knowledge shown. Trial-use: state objections on the record at the moment; no speaking objections.'},
 {id:'depoOut',grp:'Depositions',title:'Deposition master outline',ver:'v3 · 2026-04-19',scope:'generic',prov:null,
  body:'Background, employment, the incident, claimed injuries, prior and subsequent medical, daily activities and social media, damages and specials, then wind-up plus release pairs. Capture duration and expert hourly rate for a closing running total.'},
 {id:'carrier',grp:'Carrier comms',title:'Carrier letter style',ver:'v5 · 2026-05-11',scope:'generic',prov:null,
  body:'Candid defense viability; humanize the insured; a CYA paragraph documenting the hard advice given and how the insured responded; brief closings; avoid internal overshare.'},
 {id:'settle',grp:'Settlement',title:'Settlement post-acceptance framework',ver:'v2 · 2026-04-30',scope:'generic',prov:null,
  body:'Identify demand type (Holt, time-limited, offer of judgment, standard, cost-of-defense). Path A (execution-first) or Path B (payment-first). Calendar four deadlines: payment, release, coverage disclosure, dismissal.'},
 {id:'brief',grp:'Briefs & motions',title:'Brief-writing style',ver:'v3 · 2026-05-14',scope:'generic',prov:null,
  body:'Capitalize party designations for specific parties; lowercase general. No rhetorical questions; convert to declarative. Inside verbatim quotes, source casing controls; departures bracketed.'},
 {id:'billing',grp:'Billing',title:'Unified billing standard',ver:'v7 · 2026-05-01',scope:'generic',prov:null,
  body:'Single sentence, .1 increments, MM/DD/YYYY, "correspondence" not "email," specific document names. Three-part test: what, why now, why attorney vs. staff. Classify down when uncertain; avoid round even-hour entries.'},
 {id:'discFED',grp:'Discovery',title:'Discovery objection bank — Federal',ver:'v1 · 2026-05-20',scope:'fed',prov:null,
  body:'NOT PROPORTIONAL — Fed. R. Civ. P. 26(b)(1) proportionality factors. WORK PRODUCT — Rule 26(b)(3). General objections disfavored; state grounds with specificity per Rule 34(b)(2)(B) and state whether materials are withheld.'},
 {id:'discGA',grp:'Discovery',title:'Discovery objection bank — Georgia',ver:'v4 · 2026-05-18',scope:'ga-state',prov:null,
  body:'OVERBROAD / NOT PROPORTIONAL — O.C.G.A. 9-11-26(b)(1). WORK PRODUCT — 9-11-26(b)(3). VAGUE / AMBIGUOUS as to [term]. PREMATURE CONTENTION — discovery ongoing; reserve right to supplement.'},
 {id:'discFL',grp:'Discovery',title:'Discovery objection bank — Florida',ver:'v1 · 2026-05-15',scope:'fl-state',prov:null,
  body:'OVERBROAD / NOT REASONABLY CALCULATED — Fla. R. Civ. P. 1.280(b). WORK PRODUCT — 1.280(b)(4). Boilerplate disfavored; specify grounds. Note 30-day response window under Rules 1.340 / 1.350.'},
 {id:'discTN',grp:'Discovery',title:'Discovery objection bank — Tennessee',ver:'v1 · 2026-05-09',scope:'tn-state',prov:null,
  body:'OVERLY BROAD / NOT PROPORTIONAL — Tenn. R. Civ. P. 26.02. WORK PRODUCT — 26.02(3). State grounds with specificity; preserve and identify withheld materials.'},
 {id:'gacivpro',grp:'Civil procedure',title:'Georgia state civil procedure',ver:'v2 · 2026-05-12',scope:'ga-state',prov:null,
  body:'CPA Title 9 Ch. 11. 30-day answer clock; 9-11-4(h) affidavit-filing rule when an affidavit is filed more than five business days after service. State motion practice and service rules.'},
 {id:'flcivpro',grp:'Civil procedure',title:'Florida state civil procedure',ver:'v1 · 2026-05-13',scope:'fl-state',prov:null,
  body:'Fla. R. Civ. P. 20-day answer window after service; Rule 1.140 motion-to-dismiss practice; proposals for settlement under Rule 1.442 and section 768.79.'},
 {id:'tncivpro',grp:'Civil procedure',title:'Tennessee state civil procedure',ver:'v1 · 2026-05-08',scope:'tn-state',prov:null,
  body:'Tenn. R. Civ. P. 30-day answer window; Rule 12 motion practice; comparative fault pleading and the GTLA where a governmental entity is involved.'},
 {id:'ndga',grp:'Civil procedure',title:'N.D. Ga. local rules',ver:'v1 · 2026-05-19',scope:'ga-fed',prov:null,
  body:'FRCP plus N.D. Ga. Local Rules. Removal and remand timing; LR 7.1 motion practice and response deadlines; LR 56.1 statement of material facts; certificate of interested persons.'},
 {id:'sdfla',grp:'Civil procedure',title:'S.D. Fla. local rules',ver:'v1 · 2026-05-17',scope:'fl-fed',prov:null,
  body:'FRCP plus S.D. Fla. Local Rules. Removal and remand timing; L.R. 7.1 motion practice and 14-day response window; L.R. 56.1 statement of material facts.'}
];

/* ---- TASKS ---- */
const tasks={
 "Free chat (base only)":[],
 "Discovery responses":["DISC"],
 "Deposition prep":["depoObj","depoOut"],
 "Carrier evaluation letter":["carrier"],
 "Settlement / counter-offer":["settle","carrier"],
 "MSJ / motion brief":["brief"],
 "Billing entry":["billing"]
};
const STATEBANK={'ga-state':'discGA','fl-state':'discFL','tn-state':'discTN'};

/* ---- MATTERS (fictional sample data) ---- */
let matters=[
 {id:"carter",nm:"Carter v. Brightway Logistics",venue:"ga-state",ct:"Fulton State · No. 26A-04217",status:"active",issue:"deemed admissions / motor-vehicle liability",cap:null,billRule:"Standard guidelines.",pulled:false,
  ctx:[["Court","Fulton State Court, Div. 3"],["Carrier","Summit Mutual"],["Adjuster","M. Reyes · CLM-771204"],["Posture","Answer + discovery filed; depositions set"]],
  audit:{entries:12,gaps:1,note:"Route adjuster letter through LA for letterhead realization"},
  files:[["Plaintiff's First Discovery Requests","Discovery · served 04/2026",true],["Answer & Affirmative Defenses","Pleading · filed 05/2026",true],["Police report & recorded statement","Investigation · 03/2026",false],["Medical records — 286 pp.","Records · 05/2026",false]],
  wp:[{nm:"Work-product objection phrasing (refined)",dt:"edited today",elevated:false},{nm:"Deemed-admissions MSJ skeleton",dt:"2026-05-26",elevated:false}],
  corpus:[
   {type:"Case",cite:"Marsh v. Halverson Freight, 314 Ga. App. 220 (2022)",note:"Admissions deemed admitted; no excusable neglect shown.",sig:"positive",on:true},
   {type:"Statute",cite:"O.C.G.A. 9-11-36(a)",note:"Matters admitted absent timely response.",sig:"none",on:true},
   {type:"Case",cite:"Doyle v. Pruett Transport, 301 Ga. App. 9 (2019)",note:"Distinguishable; motion to withdraw admissions granted.",sig:"caution",on:false},
   {type:"Shepard's",cite:"Citator report — Marsh v. Halverson Freight",note:"No negative subsequent treatment.",sig:"positive",on:false}
  ],bills:[]},
 {id:"donnelly",nm:"Donnelly v. Maple Ridge Apartments",venue:"ga-state",ct:"DeKalb State · No. 26A-09885",status:"active",issue:"premises liability / superior knowledge",cap:null,billRule:"No travel billing (Heritage Casualty).",pulled:false,
  ctx:[["Court","DeKalb State Court"],["Carrier","Heritage Casualty"],["Opp. counsel","Harper & Lowe"],["Posture","Motion to dismiss pending"]],
  audit:{entries:9,gaps:0,note:"Clean"},
  files:[["Opposition to motion to dismiss","Brief · filed",true],["Incident & property records","Records · 2026",false],["Plaintiff's medical summary","Records · 2026",false]],
  wp:[{nm:"Impact-rule argument block",dt:"2026-05-22",elevated:false},{nm:"Open-and-obvious defense outline",dt:"2026-05-15",elevated:false}],
  corpus:[
   {type:"Case",cite:"Mercer v. Coastal Retail, 318 Ga. App. 112 (2021)",note:"Superior-knowledge; summary judgment for owner affirmed.",sig:"positive",on:true},
   {type:"Statute",cite:"O.C.G.A. 51-3-1",note:"Premises owner duty to invitees.",sig:"none",on:true},
   {type:"Case",cite:"Hadley v. Parkview Mall, 332 Ga. App. 78 (2022)",note:"Plaintiff-favorable; constructive knowledge question for jury.",sig:"warning",on:false}
  ],bills:[]},
 {id:"whitfield",nm:"Whitfield v. Eastgate Market",venue:"ga-fed",ct:"U.S. District Court, N.D. Ga.",status:"active",issue:"removal / remand — forum and amount in controversy",cap:null,billRule:"Standard guidelines.",pulled:false,
  ctx:[["Court","N.D. Ga. (removed; remand to state pending)"],["Co-counsel","Brennan Sloane"],["Claim","Premises slip-and-fall"],["Posture","Notice of Appearance filed; remand motion in play"]],
  audit:{entries:6,gaps:0,note:"Federal — track LR 7.1 / LR 56.1 deadlines"},
  files:[["Notice of Removal","Pleading · N.D. Ga.",true],["Complaint","Pleading",true],["Notice of Appearance","Filing",false],["Incident & store records","Records · 2026",false]],
  wp:[{nm:"Remand argument outline (forum / amount in controversy)",dt:"2026-05-21",elevated:false}],
  corpus:[
   {type:"Case",cite:"Sumner v. Lakeshore Stores, 41 F.4th 1180 (11th Cir. 2022)",note:"Removing party bears burden on amount in controversy.",sig:"positive",on:true},
   {type:"Statute",cite:"28 U.S.C. 1446(b)",note:"Removal timing requirements.",sig:"none",on:true},
   {type:"Shepard's",cite:"Citator report — Sumner v. Lakeshore Stores",note:"Followed; no negative treatment.",sig:"positive",on:false}
  ],bills:[]},
 {id:"alvarez",nm:"Alvarez v. Pinnacle Freight",venue:"ga-state",ct:"Gwinnett State · No. 26A-12640",status:"pending",issue:"commercial trucking / driver qualification",cap:2.0,billRule:"Research capped at 2.0 hrs per project (Cardinal Indemnity).",pulled:false,
  ctx:[["Court","Gwinnett State Court"],["Carrier","Cardinal Indemnity"],["Adjuster","D. Okafor · CLM-558031"],["Posture","Discovery; deposition prep underway"]],
  audit:{entries:8,gaps:0,note:"Clean"},
  files:[["Plaintiff's First Discovery Requests","Discovery · served 05/2026",true],["Driver qualification file","Records · 2026",false],["Telematics / ECM data","Records · 2026",false]],
  wp:[{nm:"30(b)(6) deposition topics outline",dt:"2026-05-19",elevated:false}],
  corpus:[
   {type:"Case",cite:"Whitlock v. Apex Carriers, 309 Ga. App. 511 (2020)",note:"Negligent-entrustment requires knowledge of incompetence.",sig:"positive",on:true},
   {type:"Statute",cite:"49 C.F.R. 391",note:"Driver qualification file requirements.",sig:"none",on:true}
  ],bills:[]},
 {id:"okonkwo",nm:"Okonkwo v. Sunline Residences",venue:"fl-state",ct:"FL 11th Cir. (Miami-Dade) · No. 2026-014523-CA",status:"active",issue:"premises liability / transitory foreign substance",cap:null,billRule:"Standard guidelines.",pulled:false,
  ctx:[["Court","11th Judicial Circuit, Miami-Dade"],["Carrier","Gulfstream Indemnity"],["Adjuster","P. Castellano · CLM-330417"],["Posture","Answer filed; discovery underway"]],
  audit:{entries:7,gaps:0,note:"Clean"},
  files:[["Plaintiff's First Request for Production","Discovery · served 05/2026",true],["Incident report & cleaning logs","Records · 2026",false],["Surveillance summary","Investigation · 2026",false]],
  wp:[{nm:"Section 768.0755 actual/constructive notice outline",dt:"2026-05-20",elevated:false}],
  corpus:[
   {type:"Case",cite:"Aldana v. Brightline Markets, 351 So. 3d 220 (Fla. 3d DCA 2023)",note:"768.0755 notice burden on plaintiff; SJ affirmed.",sig:"positive",on:true},
   {type:"Statute",cite:"Fla. Stat. 768.0755",note:"Transitory foreign substance — notice requirement.",sig:"none",on:true},
   {type:"Case",cite:"Pell v. Coastline Grocers, 360 So. 3d 14 (Fla. 4th DCA 2024)",note:"Plaintiff-favorable; logs created notice question.",sig:"warning",on:false}
  ],bills:[]},
 {id:"marchetti",nm:"Marchetti v. Coastal Transit",venue:"fl-fed",ct:"U.S. District Court, S.D. Fla.",status:"active",issue:"removal / diversity — fraudulent joinder",cap:null,billRule:"Standard guidelines.",pulled:false,
  ctx:[["Court","S.D. Fla. (removed)"],["Co-counsel","Devlin Park"],["Claim","Motor-vehicle / commercial bus"],["Posture","Motion to remand briefed"]],
  audit:{entries:5,gaps:0,note:"Federal — L.R. 7.1 14-day response window"},
  files:[["Notice of Removal","Pleading · S.D. Fla.",true],["Complaint","Pleading",true],["Affidavit re: in-state driver","Investigation · 2026",false]],
  wp:[{nm:"Fraudulent-joinder argument outline",dt:"2026-05-18",elevated:false}],
  corpus:[
   {type:"Case",cite:"Briggs v. Tidewater Lines, 49 F.4th 990 (11th Cir. 2023)",note:"Fraudulent-joinder standard; no possibility of recovery.",sig:"positive",on:true},
   {type:"Statute",cite:"28 U.S.C. 1332",note:"Diversity jurisdiction.",sig:"none",on:true}
  ],bills:[]},
 {id:"holloway",nm:"Holloway v. Cedar Grove Care",venue:"tn-state",ct:"TN Cir. (Davidson) · No. 26C-1180",status:"pending",issue:"long-term care / standard of care",cap:null,billRule:"Standard guidelines.",pulled:false,
  ctx:[["Court","Davidson County Circuit Court"],["Carrier","Volunteer Mutual"],["Adjuster","R. Whitfield · CLM-661092"],["Posture","Answer filed; HCLA pre-suit notice issues"]],
  audit:{entries:6,gaps:1,note:"Capture pre-suit notice review time"},
  files:[["Complaint & HCLA notice","Pleading · 2026",true],["Chart & care records","Records · 2026",false],["Certificate of good faith","Filing · 2026",false]],
  wp:[{nm:"HCLA pre-suit notice compliance outline",dt:"2026-05-17",elevated:false}],
  corpus:[
   {type:"Case",cite:"Carlin v. Highland Manor, 642 S.W.3d 311 (Tenn. 2022)",note:"HCLA pre-suit notice and certificate of good faith strictly construed.",sig:"positive",on:true},
   {type:"Statute",cite:"Tenn. Code 29-26-121",note:"HCLA pre-suit notice requirement.",sig:"none",on:true}
  ],bills:[]}
];

const activity=[
 ["Discovery objection bank — Florida added (v1)","1d ago"],
 ["Elevated: Remand argument outline → Brief-writing style (from Whitfield v. Eastgate)","3d ago"],
 ["Carter v. Brightway — 2 documents pulled from Litify","today"],
 ["Billing audit flagged gaps on Carter and Holloway","today"]
];

/* ---- STATE ---- */
let view='home', activeMatter=matters[0];
let onGuides=new Set(), jfilter='all', elevCtx=null, elevMode='append', currentTask='Discovery responses';
let matterTab='compose', addAuthOpen=false;
const MSG_DEFAULT="Draft responses and objections to Plaintiff's discovery. Flag any statutory proposition before stating it as fact.";
let msgText=MSG_DEFAULT;

function goView(v){view=v;['home','practice','matter'].forEach(x=>document.getElementById('nv-'+x).classList.toggle('on',x===v));window.scrollTo({top:0,behavior:'smooth'});render();}
function openMatter(id){activeMatter=matters.find(m=>m.id===id);matterTab='compose';addAuthOpen=false;msgText=MSG_DEFAULT;applyTask();goView('matter');}
function render(){if(view==='home')renderHome();else if(view==='practice')renderPractice();else renderMatter();}

/* ===== HOME ===== */
function renderHome(){
  const gen=[base,...library].filter(g=>g.scope==='generic').length, st=library.filter(g=>jclass(g.scope)==='state').length, fd=library.filter(g=>jclass(g.scope)==='fed').length;
  document.getElementById('app').innerHTML=`<div class="view">
   <div class="homehdr">
     <p class="eyebrow">QPWB · insurance defense</p>
     <h2 class="sec" style="font-size:30px">Drafting workspace</h2>
     <p class="lead">Litify holds the matter. Sidebar reads it, drafts against the firm's guides and the case's own legal corpus, and returns work to the file. The Claude backend is the engine the team already runs.</p>
   </div>
   <div class="statusrow">
     <div class="stat"><div class="sl">System of record</div><div class="sv slate">Litify · connected</div><div class="sd">Matters, intake, billing, document storage</div></div>
     <div class="stat"><div class="sl">Drafting engine</div><div class="sv gold">Claude backend</div><div class="sd">In use across the practice</div></div>
     <div class="stat"><div class="sl">Access</div><div class="sv good">Read-only</div><div class="sd">Write-back is a separate, gated step</div></div>
   </div>
   <div class="homecols">
     <div class="card"><h3>Matters</h3><p class="sub">${matters.length} open · open one to work the file</p>
       ${matters.map(m=>`<div class="mrow" data-act="open" data-arg="${m.id}">
          <span class="nm">${m.nm}</span><span class="ct">${m.ct}</span>
          <span class="vchip ${jclass(m.venue)}">${JL[m.venue]}</span><span class="arrow">→</span></div>`).join('')}
     </div>
     <div>
       <div class="card"><h3>Guide library</h3><p class="sub">canonical · feeds every matter</p>
         <div class="libsum">
           <div class="ls"><b>${gen+st+fd}</b><span>Total</span></div>
           <div class="ls"><b style="color:var(--good)">${gen}</b><span>Generic</span></div>
           <div class="ls"><b style="color:var(--gold)">${st}</b><span>State</span></div>
           <div class="ls"><b style="color:var(--slate)">${fd}</b><span>Federal</span></div>
         </div>
         <button class="btn ghost sm" data-act="go" data-arg="practice">Open the library →</button>
       </div>
       <div class="card" style="margin-top:18px"><h3>Recent activity</h3><p class="sub">across the practice</p>
         <div class="actfeed">${activity.map(a=>`<div class="af"><span>${a[0]}</span><span class="ad">${a[1]}</span></div>`).join('')}</div>
       </div>
     </div>
   </div>
  </div>`;
}

/* ===== PRACTICE ===== */
function matchFam(scope,fam){
  if(fam==='all')return true;
  if(fam==='generic')return scope==='generic';
  if(fam==='fed')return scope==='fed'||scope.endsWith('-fed');
  return scope.startsWith(fam);
}
function renderPractice(){
  const all=[base,...library];
  const shown=all.filter(g=>matchFam(g.scope,jfilter));
  const byGrp={};shown.forEach(g=>{if(g.id==='base')return;(byGrp[g.grp]=byGrp[g.grp]||[]).push(g);});
  let lib='';
  if(matchFam('generic',jfilter)){
    lib+=`<div class="guide locked"><div class="ghead"><span class="tier-tag">always on</span><span class="gtitle">${base.title}</span><span class="jtag generic">generic</span><span class="gver">${base.ver}</span></div></div>`;
  }
  for(const grp in byGrp){
    lib+=`<div class="grp-l">${grp}</div>`;
    lib+=byGrp[grp].map(g=>`<div class="guide"><div class="ghead">
        <span class="gtitle">${g.title}</span><span class="jtag ${jclass(g.scope)}">${JL[g.scope]||g.scope}</span><span class="gver">${g.ver}</span>
        <button class="mini-btn">edit</button></div>
        ${g.prov?`<a class="prov" data-act="open" data-arg="${g.prov.id}">↩ from ${g.prov.nm} · ${g.prov.date}</a>`:''}</div>`).join('');
  }
  const rows=matters.map(m=>`<tr data-act="open" data-arg="${m.id}">
      <td class="nm">${m.nm}</td>
      <td><span class="vchip ${jclass(m.venue)}">${JL[m.venue]}</span></td>
      <td style="text-align:center">${m.audit.entries}</td>
      <td class="${m.audit.gaps?'gapy':'gapn'}" style="text-align:center">${m.audit.gaps?m.audit.gaps+' flagged':'clean'}</td>
      <td class="ct" style="color:var(--faint);font-size:11px">${m.audit.note}</td></tr>`).join('');

  document.getElementById('app').innerHTML=`<div class="view">
   <p class="eyebrow l" style="margin-top:30px">Global · matter-neutral</p>
   <h2 class="sec">Practice</h2>
   <p class="lead">Templates, guides, and cross-matter billing in one place. Edit a guide here and every matter inherits it. The library is layered by jurisdiction, so generic standards stay portable while each state's and district's procedure stays in its own overlay.</p>
   <div class="pgrid">
     <div>
       <div class="card"><h3>Matters</h3><p class="sub">${matters.length} open · click to enter the case workspace</p>
         ${matters.map(m=>`<div class="mrow" data-act="open" data-arg="${m.id}">
            <span class="nm">${m.nm}</span><span class="ct">${m.ct}</span>
            <span class="vchip ${jclass(m.venue)}">${JL[m.venue]}</span><span class="arrow">→</span></div>`).join('')}
       </div>
       <div class="card" style="margin-top:18px"><h3>Billing audit</h3><p class="sub">cross-matter · applies the unified billing standard</p>
         <table class="audit"><thead><tr><th>Matter</th><th>Venue</th><th style="text-align:center">Entries</th><th style="text-align:center">Gaps</th><th>Realization note</th></tr></thead>
         <tbody>${rows}</tbody></table></div>
     </div>
     <div class="card"><h3>Guide library</h3><p class="sub">canonical · single source of truth · feeds every matter</p>
       <div class="jfilter">
         ${[['all','All'],['generic','Generic'],['ga','Georgia'],['fl','Florida'],['tn','Tennessee'],['fed','Federal']].map(j=>`<button class="${jfilter===j[0]?'on':''}" data-act="jf" data-arg="${j[0]}">${j[1]}</button>`).join('')}
       </div>
       ${lib}
     </div>
   </div>
  </div>`;
}
function setJ(j){jfilter=j;render();}

/* ===== MATTER ===== */
function availGuides(m){return library.filter(g=>g.scope==='generic'||g.scope===m.venue||(g.scope==='fed'&&m.venue.endsWith('-fed')));}
function discFor(m){return m.venue.endsWith('-fed')?'discFED':(STATEBANK[m.venue]||'discGA');}
function applyTask(){
  const sel=document.getElementById('taskSel');
  currentTask=sel?sel.value:currentTask;
  const raw=tasks[currentTask]||[];
  onGuides=new Set(raw.map(id=>id==='DISC'?discFor(activeMatter):id));
}
function isAuto(id){const raw=tasks[currentTask]||[];return raw.map(x=>x==='DISC'?discFor(activeMatter):x).includes(id);}
function togGuide(id){onGuides.has(id)?onGuides.delete(id):onGuides.add(id);render();}

function computeBudget(m){
  let txt=base.body.length;
  availGuides(m).filter(g=>onGuides.has(g.id)).forEach(g=>txt+=g.body.length);
  txt+=m.ctx.map(c=>c[0]+': '+c[1]).join('\n').length;
  if(m.pulled)txt+=m.files.filter(f=>f[2]).map(f=>f[0]).join('\n').length;
  txt+=msgText.length;
  const authOn=m.corpus.filter(a=>a.on);
  const authTok=authOn.reduce((s,a)=>s+(AUTHW[a.type]||300),0);
  const tok=Math.round(txt/4)+authTok;
  return {tok,guides:onGuides.size,docs:m.pulled?m.files.filter(f=>f[2]).length:0,auth:authOn.length};
}

function renderMatter(){
  const m=activeMatter, b=computeBudget(m);
  const tabs=[['compose','Compose',''],['corpus','Legal corpus',m.corpus.filter(a=>a.on).length+'/'+m.corpus.length],['docs','Documents',m.pulled?m.files.filter(f=>f[2]).length+'':'·'],['billing','Billing',m.bills.length?m.bills.length+'':'·'],['wp','Work product',m.wp.filter(w=>!w.elevated).length+'']];
  const barCol=b.tok<2500?'var(--good)':b.tok<7000?'var(--gold)':'var(--oxblood)';

  document.getElementById('app').innerHTML=`<div class="view">
   <div class="crumb"><a data-act="go" data-arg="home">Home</a> / <a data-act="go" data-arg="practice">Practice</a> / <span style="color:var(--slate)">${m.nm}</span></div>
   <div class="mhead">
     <div><h2>${m.nm}</h2><div class="mt">${m.ct} · ${m.issue}</div></div>
     <div class="mswitch">${matters.map(x=>`<button class="${x.id===m.id?'on':''}" data-act="open" data-arg="${x.id}">${x.nm.split(' v.')[0]}</button>`).join('')}</div>
   </div>
   <div class="mtabs">${tabs.map(t=>`<button class="mtab-btn ${matterTab===t[0]?'on':''}" data-act="tab" data-arg="${t[0]}">${t[1]}<span class="cc">${t[2]}</span></button>`).join('')}</div>
   <div class="mcols">
     <div>
       <p class="eyebrow m">Auto-attached context</p>
       ${m.ctx.map(c=>`<span class="ctx-chip"><span class="lk">ALWAYS ON</span><b>${c[0]}:</b> ${c[1]}</span>`).join('')}
       <div class="vbanner" style="margin-top:12px">▣ Venue: <b>${JL[m.venue]}</b></div>
       <div class="promptsum">
         <div class="pt">In the assembled prompt</div>
         <div class="pr"><span>Base standard</span><b>on</b></div>
         <div class="pr"><span>Library guides</span><b>${b.guides}</b></div>
         <div class="pr"><span>Litify documents</span><b>${b.docs}</b></div>
         <div class="pr"><span>Corpus authorities</span><b>${b.auth}</b></div>
         <div class="pr" style="border-top:1px solid var(--rule);margin-top:5px;padding-top:8px"><span>Context budget</span><b>${b.tok.toLocaleString()} tok</b></div>
         <div class="pbar"><i style="width:${Math.min(100,b.tok/250)}%;background:${barCol}"></i></div>
       </div>
     </div>
     <div>${tabHTML(m)}</div>
   </div>
  </div>`;
  if(matterTab==='compose')renderSegs();
}
function setTab(t){matterTab=t;addAuthOpen=false;render();}

function tabHTML(m){
  if(matterTab==='compose')return tabCompose(m);
  if(matterTab==='corpus')return tabCorpus(m);
  if(matterTab==='docs')return tabDocs(m);
  if(matterTab==='billing')return tabBilling(m);
  return tabWP(m);
}

/* --- COMPOSE --- */
function tabCompose(m){
  const avail=availGuides(m), byGrp={};avail.forEach(g=>{(byGrp[g.grp]=byGrp[g.grp]||[]).push(g);});
  let libHTML=`<div class="gtoggle locked"><div class="gth"><span class="gcbx">${check}</span><span class="sgt">${base.title}</span><span class="auto-f" style="color:var(--gold)">base</span></div></div>`;
  for(const grp in byGrp){
    libHTML+=`<div class="grp-l">${grp}</div>`;
    libHTML+=byGrp[grp].map(g=>{const on=onGuides.has(g.id);
      return `<div class="gtoggle ${on?'on':''}"><div class="gth" data-act="tog" data-arg="${g.id}">
        <span class="gcbx">${on?check:''}</span><span class="sgt">${g.title}</span>
        <span class="jtag ${jclass(g.scope)}" style="margin-left:auto">${JL[g.scope]||g.scope}</span>
        ${on&&isAuto(g.id)?'<span class="auto-f">auto</span>':''}</div></div>`;
    }).join('');
  }
  return `<div class="composecols">
    <div>
      <p class="eyebrow l">Compose</p>
      <select id="taskSel" data-change="task">
        ${Object.keys(tasks).map(t=>`<option ${t===currentTask?'selected':''}>${t}</option>`).join('')}
      </select>
      <div class="vbanner">▣ Library filtered to generic + ${JL[m.venue]} guides.</div>
      <p class="grp-l" style="margin-top:0">Guide library</p>
      ${libHTML}
    </div>
    <div>
      <p class="eyebrow">Assembled prompt</p>
      <div class="guard-note"><div style="color:var(--oxblood);font-size:15px">⚖</div><div><div class="ct">Citation guardrail · always on</div><div class="cb">Authorities are flagged unverified until confirmed in Lexis+. The draft does not assert a cite on the engine's word.</div></div></div>
      <div class="meter"><div class="meter-top"><span>Context budget</span><b id="tok">0 tok</b></div><div class="bar"><i id="barFill"></i></div><div class="meter-note" id="meterNote"></div></div>
      <div id="segs"></div>
      <div class="composer">
        <textarea id="msg" data-input="msg">${msgText}</textarea>
        <div class="send-row"><button class="send" title="Disabled in prototype">Send to Claude →</button><span class="send-note">prototype — assembly preview</span></div>
      </div>
    </div>
  </div>`;
}

function renderSegs(){
  const m=activeMatter;
  const segs=[{cls:'base',role:'SYSTEM',title:base.title,ver:base.ver,body:base.body}];
  availGuides(m).filter(g=>onGuides.has(g.id)).forEach(g=>segs.push({cls:'lib',role:'REFERENCE',title:g.title,ver:g.ver,body:g.body}));
  const authOn=m.corpus.filter(a=>a.on);
  if(authOn.length){
    const ab=authOn.map(a=>'• ['+a.type+'] '+a.cite+(a.note?' — '+a.note:'')+(a.type==='Case'?' [full text in context]':'')).join('\n');
    segs.push({cls:'auth',role:'AUTHORITY',title:'Legal corpus — '+authOn.length+' included',ver:'unverified',body:ab});
  }
  let ctxBody=m.ctx.map(c=>c[0]+': '+c[1]).join('\n');
  if(m.pulled){const sel=m.files.filter(f=>f[2]);ctxBody+='\n— pulled from Litify —\n'+sel.map(f=>'• '+f[0]).join('\n');}
  segs.push({cls:'matter',role:'CONTEXT',title:m.nm+' — matter file',ver:'live',body:ctxBody});
  segs.push({cls:'task',role:'USER',title:'Task: '+currentTask,ver:'',body:msgText});

  document.getElementById('segs').innerHTML=segs.map((s,i)=>`<div class="seg ${s.cls}" id="sg${i}">
     <div class="sgh" data-act="seg" data-arg="${i}">
       <span class="chev">▶</span><span class="role">${s.role}</span><span class="sgt">${s.title}</span><span class="sgv">${s.ver}</span></div>
     <div class="sgb" id="sb${i}">${s.body.replace(/</g,'&lt;')}</div></div>`).join('');

  const b=computeBudget(m);
  document.getElementById('tok').textContent=b.tok.toLocaleString()+' tok';
  const fill=document.getElementById('barFill');fill.style.width=Math.min(100,b.tok/250)+'%';
  fill.style.background=b.tok<2500?'var(--good)':b.tok<7000?'var(--gold)':'var(--oxblood)';
  document.getElementById('meterNote').textContent = b.tok>7000
    ? 'Heavy — full-text authorities dominate the budget. Include only what the motion relies on.'
    : (b.guides<=3?`${b.guides} guides, ${b.auth} authorities loaded — lean.`:`${b.guides} guides loaded — attention spreads thin past ~3.`);
}

/* --- DOCUMENTS --- */
function tabDocs(m){
  return `<div style="max-width:620px">
    <div class="lh">▣ LITIFY · documents (read-only)</div>
    ${m.pulled
      ? m.files.map((f,i)=>`<div class="file ${f[2]?'on':''}" data-act="togf" data-arg="${i}"><span class="cbx">${check}</span><span class="fn">${f[0]}</span><span class="fm">${f[1]}</span></div>`).join('')
        +`<p class="litnote">Read-only · scoped to your Litify permissions. ${m.files.filter(f=>f[2]).length} of ${m.files.length} selected; selected documents feed the assembled prompt.</p>`
      : `<button class="btn sm" data-act="pull">Pull from Litify →</button>
         <p class="litnote">Resolves this matter in Litify and lists its documents with details. You approve which documents to pull; nothing transfers until you choose. Selected documents become available to the assembled prompt.</p>`}
  </div>`;
}
function pullLitify(){activeMatter.pulled=true;render();}
function togF(i){activeMatter.files[i][2]=!activeMatter.files[i][2];render();}

/* --- LEGAL CORPUS --- */
function tabCorpus(m){
  const items=m.corpus.map((a,i)=>`<div class="auth ${a.on?'on':''}" data-act="toga" data-arg="${i}">
     <span class="acbx">${check}</span>
     <span class="sig ${a.sig}" title="treatment"></span>
     <div class="ainfo"><div class="acite">${a.cite}</div>${a.note?`<div class="anote">${a.note}</div>`:''}</div>
     <span class="atype">${a.type}</span></div>`).join('');
  return `<div>
    <div class="lexgen">
      <h4>Generate Lexis+ searches</h4>
      <p class="ls">Issue: ${m.issue}. Produces a citation list to paste into Lexis+ and separate boolean queries for additional authority.</p>
      <button class="btn sm" data-act="lex">Generate searches →</button>
      ${m.lexout?`<div class="lexout">${m.lexout}</div><p class="lexnote">Illustrative output. In use, the engine returns live citations; pull them and Shepardize in Lexis+, then add results below for the draft.</p>`:''}
    </div>
    <p class="eyebrow l">Case corpus — authority held in this matter</p>
    <div class="sigkey"><span><span class="sig positive"></span>positive / followed</span><span><span class="sig caution"></span>caution / distinguished</span><span><span class="sig warning"></span>negative / questioned</span><span><span class="sig none"></span>statute or memo</span></div>
    ${items}
    <button class="btn ghost sm" style="margin-top:6px" data-act="addtog">${addAuthOpen?'Close':"+ Add authority (paste a pulled case, statute, Shepard's report, or memo)"}</button>
    ${addAuthOpen?`<div class="addauth">
       <div class="row2"><select class="fin" id="aaType"><option>Case</option><option>Statute</option><option>Shepard's</option><option>Memo</option></select>
       <select class="fin" id="aaSig"><option value="none">no signal</option><option value="positive">positive</option><option value="caution">caution</option><option value="warning">negative</option></select></div>
       <input class="fin" id="aaCite" placeholder="Citation or title (e.g., Smith v. Jones, 300 Ga. App. 1)">
       <input class="fin" id="aaNote" placeholder="One-line note on relevance or treatment">
       <button class="btn sm" data-act="addauth">Add to corpus</button>
     </div>`:''}
    <p class="litnote" style="margin-top:12px">Checked authorities are injected into the assembled prompt as an AUTHORITY block, flagged unverified. Full-text cases are heavy, so the context budget reflects what you include.</p>
  </div>`;
}
function togAuth(i){activeMatter.corpus[i].on=!activeMatter.corpus[i].on;render();}
function toggleAddAuth(){addAuthOpen=!addAuthOpen;render();}
function addAuth(){
  const t=document.getElementById('aaType').value, c=document.getElementById('aaCite').value.trim(), n=document.getElementById('aaNote').value.trim(), s=document.getElementById('aaSig').value;
  if(!c){return;}
  activeMatter.corpus.push({type:t,cite:c,note:n,sig:s,on:true});
  addAuthOpen=false;render();toast(`Added <b>${t}</b> to the ${activeMatter.nm.split(' v.')[0]} corpus`);
}
function genLexis(){
  const m=activeMatter, i=m.issue.toLowerCase();
  let cites, bq;
  if(i.includes('admission')){
    cites='Marsh v. Halverson Freight, 314 Ga. App. 220 (2022); Doyle v. Pruett Transport, 301 Ga. App. 9 (2019)';
    bq=['("requests for admission" or "deemed admitted") w/s "summary judgment" and court(ga)','"withdraw" w/3 admission! and "excusable neglect" and date(aft 2015)','O.C.G.A. /5 "9-11-36" and admitted'];
  }else if(i.includes('premises')||i.includes('foreign substance')){
    cites='Mercer v. Coastal Retail, 318 Ga. App. 112 (2021); Aldana v. Brightline Markets, 351 So. 3d 220 (Fla. 3d DCA 2023)';
    bq=['premises w/5 (liabilit! or negligen!) and "superior knowledge" and summary judgment','(slip or trip or fall) w/s ("constructive knowledge" or "actual knowledge")','invitee w/10 "static condition" and date(aft 2016)'];
  }else if(i.includes('remand')||i.includes('removal')||i.includes('joinder')){
    cites='Sumner v. Lakeshore Stores, 41 F.4th 1180 (11th Cir. 2022); Briggs v. Tidewater Lines, 49 F.4th 990 (11th Cir. 2023)';
    bq=['remand w/s "amount in controversy" and "burden" and court(11th)','"fraudulent joinder" w/10 ("no possibility" or "no reasonable basis")','28 U.S.C. /5 1446 and "thirty days"'];
  }else if(i.includes('trucking')||i.includes('driver')){
    cites='Whitlock v. Apex Carriers, 309 Ga. App. 511 (2020)';
    bq=['"negligent entrustment" w/s (knowledge or "should have known") and date(aft 2015)','"driver qualification file" or "49 C.F.R. 391"','spoliation w/10 (telematics or "ECM" or "ELD")'];
  }else if(i.includes('care')||i.includes('hcla')){
    cites='Carlin v. Highland Manor, 642 S.W.3d 311 (Tenn. 2022)';
    bq=['"pre-suit notice" w/s "29-26-121" and (dismiss! or compliance)','"certificate of good faith" w/10 "29-26-122"','HCLA w/5 "substantial compliance" and date(aft 2018)'];
  }else{
    cites='[issue-specific citations]';
    bq=['[term] w/s [term] and court(ga)','[concept] w/10 "summary judgment"'];
  }
  m.lexout=`<span class="lbl">Citations to pull (paste into Lexis+)</span>${cites}\n<span class="lbl">Lexis+ boolean searches</span>`+bq.map((q,k)=>(k+1)+'. '+q).join('\n');
  render();
}

/* --- BILLING --- */
function todayMD(){const d=new Date();const p=n=>String(n).padStart(2,'0');return p(d.getMonth()+1)+'/'+p(d.getDate())+'/'+d.getFullYear();}
function billingFor(task,m){
  const d=todayMD();
  const adjC=m.ctx.find(c=>c[0]==='Adjuster');const adj=adjC?adjC[1].split(' · ')[0]:null;
  const corr=adj?[d,"0.3","Correspondence to adjuster "+adj+" regarding case strategy."]:null;
  let rows;
  switch(task){
    case "Discovery responses":
      rows=[[d,"1.3","Prepare Defendant's responses and objections to Plaintiff's first interrogatories and requests for production."],[d,"0.4","Review file documents to confirm factual basis for discovery responses."]];break;
    case "Deposition prep":
      rows=[[d,"1.6","Prepare outline and exhibits for the deposition of Plaintiff."],[d,"0.9","Review medical records and prior statements in advance of the deposition."]];break;
    case "Carrier evaluation letter":
      rows=[[d,"0.8","Prepare evaluation letter to carrier addressing liability, damages, and settlement posture."]];break;
    case "Settlement / counter-offer":
      rows=[[d,"0.6","Prepare counteroffer correspondence and confirm settlement conditions."]];break;
    case "MSJ / motion brief":
      rows=[[d,"1.4","Draft Defendant's motion for summary judgment and supporting brief."],[d,"0.7","Develop Lexis+ search strings and review pulled authority for the motion."]];if(corr)rows.push(corr);break;
    case "Billing entry":
      rows=[[d,"0.2","Review file and update billing records."]];break;
    default:
      rows=[[d,"0.3","Review file and correspondence."]];
  }
  if(m.cap){rows=rows.map(r=>parseFloat(r[1])>m.cap?[r[0],m.cap.toFixed(1),r[2]]:r);}
  return rows;
}
function genBilling(){
  const m=activeMatter;
  const rows=billingFor(currentTask,m);
  m.bills=rows.map(r=>({d:r[0],h:r[1],n:r[2]}));
  render();toast(`Generated ${rows.length} billable entr${rows.length===1?'y':'ies'} for ${m.nm.split(' v.')[0]}`);
}
function tabBilling(m){
  const total=m.bills.reduce((s,e)=>s+parseFloat(e.h),0).toFixed(1);
  const carrier=m.ctx.find(c=>c[0]==='Carrier');
  return `<div style="max-width:680px">
    <p class="eyebrow l">Billable entries</p>
    <p class="lead" style="margin-bottom:14px">Generates entries for the current Compose task (<b style="color:var(--paper)">${currentTask}</b>), formatted to the unified billing standard and ${carrier?('the '+carrier[1]+' profile'):'the carrier profile'}.</p>
    <button class="btn sm" data-act="bill">Generate entries from this task →</button>
    ${m.bills.length?`<table class="billtbl"><thead><tr><th>Date</th><th style="text-align:center">Hours</th><th>Narrative</th></tr></thead>
      <tbody>${m.bills.map(e=>`<tr><td class="bd">${e.d}</td><td class="bh">${e.h}</td><td>${e.n}</td></tr>`).join('')}
      <tr><td class="bd"></td><td class="bh">${total}</td><td style="color:var(--faint);font-size:11px">total</td></tr></tbody></table>
      <p class="billrule"><b>Format applied:</b> single-sentence, .1 increments, MM/DD/YYYY, "correspondence" not "email," specific document names, non-round hours. <b>Carrier:</b> ${m.billRule}</p>
      <div class="send-row" style="margin-top:14px"><button class="send" title="Disabled in prototype">Post to Litify →</button><span class="send-note">write-back gated · review before posting</span></div>`
    :`<p class="litnote" style="margin-top:12px">No entries yet. Generate from the current task; review and edit before anything posts to Litify.</p>`}
  </div>`;
}

/* --- WORK PRODUCT --- */
function tabWP(m){
  return `<div style="max-width:680px">
    <p class="eyebrow l">Work product — elevation candidates</p>
    <p class="lead" style="margin-bottom:14px">Reusable work made in this matter. Elevate copies it up to the Practice library with provenance and an optional genericize pass.</p>
    ${m.wp.map((w,i)=>`<div class="wp"><div class="wt"><div class="nm">${w.nm}</div><div class="dt">${w.dt}</div></div>
       ${w.elevated?'<button class="elev done">✓ in library</button>':`<button class="elev" data-act="elev" data-arg="${m.id}|${i}">⤴ Elevate</button>`}</div>`).join('')}
  </div>`;
}

/* ===== ELEVATION ===== */
function openElevation(mid,i){
  const m=matters.find(x=>x.id===mid);elevCtx={m,i,w:m.wp[i]};elevMode='append';
  document.getElementById('mSrc').textContent='“'+elevCtx.w.nm+'”';
  document.getElementById('mProv').textContent='from '+m.nm+' · '+new Date().toISOString().slice(0,10);
  document.getElementById('dst').innerHTML=library.map(g=>`<option value="${g.id}">${g.title} (${JL[g.scope]||g.scope})</option>`).join('');
  guessDst();setMode('append');document.getElementById('gen').classList.add('on');
  document.getElementById('scrim').classList.add('on');
}
function guessDst(){const n=elevCtx.w.nm.toLowerCase();let p='brief';
  if(n.includes('depo'))p='depoOut';else if(n.includes('settle'))p='settle';
  else if(n.includes('objection'))p=discFor(elevCtx.m);
  document.getElementById('dst').value=p;}
function setMode(mode){elevMode=mode;document.getElementById('optAppend').classList.toggle('sel',mode==='append');document.getElementById('optNew').classList.toggle('sel',mode==='new');document.getElementById('dstWrap').style.display=mode==='append'?'block':'none';}
function closeModal(){document.getElementById('scrim').classList.remove('on');}
function commitElevation(){
  const {m,i,w}=elevCtx, gen=document.getElementById('gen').classList.contains('on');
  const prov={id:m.id,nm:m.nm,date:new Date().toISOString().slice(0,10)};
  if(elevMode==='append'){const g=library.find(x=>x.id===document.getElementById('dst').value);
    const n=parseInt((g.ver.match(/v(\d+)/)||[])[1]||1)+1;g.ver='v'+n+' · '+prov.date;g.prov=prov;
    toast(`Merged into <b>${g.title}</b>${gen?' · genericized':''} — now in the Practice library`);
  }else{const id='t'+Date.now();
    library.push({id,grp:'Elevated templates',title:w.nm.replace(/\s*\(refined\)/i,''),ver:'v1 · '+prov.date,scope:'generic',prov});
    toast(`Created template <b>${w.nm}</b>${gen?' · genericized':''} — now in the Practice library`);}
  m.wp[i].elevated=true;closeModal();render();
}
function toast(html){const t=document.getElementById('toast');t.innerHTML=html;t.classList.add('on');setTimeout(()=>t.classList.remove('on'),3800);}

/* ===== EVENT DELEGATION (replaces inline handlers for CSP) ===== */
const ACT={
  go:(a)=>goView(a),
  open:(a)=>openMatter(a),
  jf:(a)=>setJ(a),
  tab:(a)=>setTab(a),
  tog:(a)=>togGuide(a),
  seg:(a)=>{document.getElementById('sg'+a).classList.toggle('open');document.getElementById('sb'+a).classList.toggle('open');},
  pull:()=>pullLitify(),
  togf:(a)=>togF(+a),
  lex:()=>genLexis(),
  toga:(a)=>togAuth(+a),
  addtog:()=>toggleAddAuth(),
  addauth:()=>addAuth(),
  bill:()=>genBilling(),
  elev:(a)=>{const parts=a.split('|');openElevation(parts[0],+parts[1]);},
  mode:(a)=>setMode(a),
  gentog:()=>document.getElementById('gen').classList.toggle('on'),
  close:()=>closeModal(),
  commit:()=>commitElevation()
};
document.addEventListener('click',function(e){
  const t=e.target.closest('[data-act]');
  if(!t)return;
  const fn=ACT[t.getAttribute('data-act')];
  if(fn){e.preventDefault();fn(t.getAttribute('data-arg'));}
});
document.addEventListener('change',function(e){
  if(e.target.getAttribute && e.target.getAttribute('data-change')==='task'){applyTask();render();}
});
document.addEventListener('input',function(e){
  if(e.target.getAttribute && e.target.getAttribute('data-input')==='msg'){msgText=e.target.value;renderSegs();}
});

/* ===== INIT ===== */
applyTask();
goView('home');
