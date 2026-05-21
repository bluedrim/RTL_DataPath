# RTL DataPath Visualizer

`rte` 스타일 filelist(`.f`)를 읽어서,
1) 전체 모듈 계층 연결(누가 누구를 인스턴스 하는지)
2) 주요 data path 후보(이름 기반 휴리스틱)
를 한 번에 그림으로 만들어주는 스크립트입니다.

## 사용 방법

```bash
python3 rtl_datapath_visualizer.py <filelist.f> [--top TOP_MODULE] [--out rtl_datapath.dot] [--png rtl_datapath.png]
```

예시:

```bash
python3 rtl_datapath_visualizer.py ./rte/filelist.f --top top
```

## 출력물

- `rtl_datapath.dot`: Graphviz DOT 소스
- `rtl_datapath.png`: Graphviz `dot`가 설치되어 있으면 자동 생성

## 표시 규칙

- **파란색 노드**: top module
- **주황색 노드/엣지**: data path 가능성이 높은 모듈/연결
  - 이름에 `data`, `alu`, `mul`, `adder`, `fifo`, `regfile`, `pipe`, `mem` 등의 키워드가 포함된 경우
- **회색 노드/엣지**: 일반 제어/기타 연결

## 지원 filelist 문법

- Verilog/SystemVerilog 파일 경로 (`.v`, `.sv`, `.vh`, `.svh`)
- `+incdir+...` (파싱은 무시)
- `-v <file>`
- 주석/빈 줄

> 참고: 매우 복잡한 매크로/생성문(`generate`) 기반 구조에서는 인스턴스 파싱이 100% 정확하지 않을 수 있습니다.
