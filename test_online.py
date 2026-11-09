"""Имитация сетевой партии: несколько клиентов по WebSocket играют до конца."""
import asyncio, json, os, random, sys
os.environ.update(DEV_AUTH="1", GAME_SPEED=os.getenv("GAME_SPEED","0.03"), PORT=os.getenv("PORT","8765"))
sys.path.insert(0, os.path.dirname(__file__))
import aiohttp, app, logging
logging.getLogger('aiohttp.access').setLevel(logging.WARNING)

URL = f"http://127.0.0.1:{os.environ['PORT']}/ws"

async def client(i, code_box, results, n_humans, fill):
    st = {}
    async with aiohttp.ClientSession() as s, s.ws_connect(URL) as ws:
        await ws.send_json({"t":"hello","dev":{"id":1000+i,"first_name":f"Игрок{i}"},"room":code_box.get("code","") if i else ""})
        if i==0: await ws.send_json({"t":"create"})
        myc=None; errors=[]; acted=set(); chats=0; photos=0
        async for msg in ws:
            d = json.loads(msg.data); t=d["t"]
            if t=="joined":
                myc=d["code"]
                if i==0: code_box["code"]=d["code"]; code_box["ev"].set()
                if i==1: await ws.send_json({"t":"photo","data":"data:image/jpeg;base64,AAAA"})
                continue
            if t=="photo": photos+=1
            if t=="error": errors.append(d["text"])
            if t=="chat": chats+=1
            if t!="state": continue
            if d["code"]!=myc: continue
            st=d; ph=d["phase"]; key=(ph,d["round"]); LAST[i]=(ph,d['round'],d['code'],len(d['players']),d['my']['alive'] if d.get('my') else None)
            me=next(p for p in d["players"] if p["id"]==d["me"])
            if ph=="lobby":
                if i==0 and len(d["players"])>=n_humans and key not in acted:
                    acted.add(key); await ws.send_json({"t":"start","bots":fill})
                continue
            if ph=="final":
                results[i]=(d["final"]["winner"], d["final"]["rounds"], d["my"]["pts"], errors, chats, photos); break
            if not me["alive"] or key in acted: continue
            acted.add(key)
            role=d["my"]["role"]; alive=[p for p in d["players"] if p["alive"] and p["id"]!=me["id"]]
            await asyncio.sleep(random.random()*0.05)
            if ph=="night":
                if role=="tourist": await ws.send_json({"t":"mem","ok":random.random()<.6})
                else:
                    c=[p for p in alive if not (role=="saboteur" and p["role"]=="saboteur")]
                    if role=="mechanic": c=c+[me]
                    await ws.send_json({"t":"night","target":random.choice(c)["id"]})
            elif ph=="day":
                tk=d["my"]["task"]
                await ws.send_json({"t":"chat","text":f"Я думаю это {random.choice(alive)['name']}? 🔥 согласен! 12 км"})
                if tk and tk["kind"]=="photo": await ws.send_json({"t":"chat","text":"","img":"data:image/jpeg;base64,BBBB"})
                await ws.send_json({"t":"ready"})
            elif ph in ("morning","result"): await ws.send_json({"t":"ready"})
            elif ph=="contest":
                c=d["contest"]
                if c["type"] in ("react","stop"): await ws.send_json({"t":"contest","times":[random.randint(200,6000) for _ in range(2)]})
                else: await ws.send_json({"t":"contest","answers":[random.randrange(len(q["o"])) for q in c["qs"]]})
            elif ph=="vote":
                opts=[p for p in alive if p["id"]!=d["vote"]["immune"]]
                await ws.send_json({"t":"vote","target":random.choice(opts)["id"] if opts and random.random()<.9 else -1})
        if i not in results: results[i]=("dead-end",st.get("phase"),0,errors,chats,photos)

async def game(n_humans, fill):
    box={"ev":asyncio.Event()}; results={}
    first=asyncio.create_task(client(0,box,results,n_humans,fill))
    await box["ev"].wait()
    others=[asyncio.create_task(client(i,box,results,n_humans,fill)) for i in range(1,n_humans)]
    await asyncio.wait_for(asyncio.gather(first,*others),timeout=float(os.getenv('TMO','40')))
    return results

LAST={}
CASES=eval(os.getenv('CASES','[(5,False)]'))
async def main():
    task=asyncio.create_task(app.main()); await asyncio.sleep(0.5)
    ok=0; total=0
    for n,fill in CASES:
        total+=1
        try:
            r=await game(n,fill)
            w={v[0] for v in r.values()}; errs=[e for v in r.values() for e in v[3]]
            good = w <= {"camp","saboteurs"} and len(w)==1
            ok+=good
            print(f"игроков {n:2} боты={fill!s:5} → {w} раундов={r[0][1]} очки={[v[2] for v in r.values()]} чат={r[0][4]} фото={r[0][5]} ошибки={errs[:2]}")
        except Exception as e:
            print("СБОЙ",n,fill,repr(e),LAST); r=app.ROOMS; print({c:(x.phase,x.round,[(p.id,p.alive,p.online) for p in x.players]) for c,x in list(r.items())[-1:]})
    print(f"успешно {ok} из {total}, комнат открыто {len(app.ROOMS)}")
    task.cancel()
asyncio.run(main())
