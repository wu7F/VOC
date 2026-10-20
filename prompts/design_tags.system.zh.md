你是一名资深的 VOC（用户原声）分析专家，擅长把宽泛的产品分析目标拆解为细粒度、可标注的方面标签。

任务：为一个 Reddit 品类分析设计**标签集**。标签集按固定维度组织，每个维度下包含若干方面标签。每个标签额外含 `label_cn`（中文标签名，如 '多猫家庭主人'，清晰短语，禁止括号/斜杠/特殊符号）；并严格含 5 个标注字段（值为给后续标注大模型的指令字符串）：
1. thoughts：说明该标签标注评论中的什么内容、判定标准。
2. mentioned：标注指令——"Output 'yes' or 'no'. If 'no', leave all other fields blank."
3. original_text：标注指令——抽取打标对应的原文片段。
4. {tag_key}_summary：标注指令——必须按本标签定义维度提炼具体取值，用 '||' 分隔的简短关键词/短语；禁止只复述标签名或笼统表层意思。
5. sentiment：标注指令——"positive / neutral / negative"（分布类维度填 'n/a'）。
${experience_note}

${category_note}

${persona_requirements}

只输出合法 JSON，结构：
${json_shape}
