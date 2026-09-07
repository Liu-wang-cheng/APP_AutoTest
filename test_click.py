import uiautomator2 as u2, re, time
d = u2.connect('127.0.0.1:5555')

# 模拟完整流程：先点吸力 → 等3秒 → 找安静
el = d(textContains='吸力')
info = el.info
xml = d.dump_hierarchy()

# 找 Suck_Row
for rm in re.finditer(r'content-desc="(\w+_Row)"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml):
    row_start = rm.start()
    row_end = xml.find('content-desc="', rm.end())
    if row_end < 0: row_end = len(xml)
    if '吸力' in xml[row_start:row_end]:
        d(description=rm.group(1)).click()
        print(f'点击了 {rm.group(1)}')
        time.sleep(3)
        break

# 现在找安静
xml2 = d.dump_hierarchy()
all_texts = set(re.findall(r'text="([^"]+)"', xml2))
print(f'弹窗文本: {sorted(t for t in all_texts if t.strip())}')

# 尝试各种方式找安静
for method in ['textContains', 'text', 'description']:
    try:
        if method == 'textContains':
            el2 = d(textContains='安静')
        elif method == 'text':
            el2 = d(text='安静')
        else:
            el2 = d(description='安静')
        if el2.exists(timeout=1):
            print(f'{method}=安静: exists=True, clickable={el2.info.get("clickable")}, bounds={el2.info.get("bounds")}')
            el2.click()
            print('点击成功!')
            break
    except Exception as e:
        print(f'{method}=安静: {e}')
