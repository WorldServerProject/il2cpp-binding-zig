import gc
import sys
from clang import cindex
from clang.cindex import CursorKind, TypeKind

# 常见 C 基本类型到 Zig 的映射字典
PRIMITIVE_MAP = {
    TypeKind.VOID: 'void',
    TypeKind.BOOL: 'bool',
    TypeKind.CHAR_S: 'i8',
    TypeKind.CHAR_U: 'u8',
    TypeKind.SCHAR: 'i8',
    TypeKind.UCHAR: 'u8',
    TypeKind.SHORT: 'i16',
    TypeKind.USHORT: 'u16',
    TypeKind.INT: 'c_int',
    TypeKind.UINT: 'c_uint',
    TypeKind.LONG: 'c_long',
    TypeKind.ULONG: 'c_ulong',
    TypeKind.LONGLONG: 'i64',
    TypeKind.ULONGLONG: 'u64',
    TypeKind.FLOAT: 'f32',
    TypeKind.DOUBLE: 'f64',
}

# stdint / POSIX 常见 Typedef 映射
TYPEDEF_OVERRIDE = {
    'uint8_t': 'u8',
    'uint16_t': 'u16',
    'uint32_t': 'u32',
    'uint64_t': 'u64',
    'int8_t': 'i8',
    'int16_t': 'i16',
    'int32_t': 'i32',
    'int64_t': 'i64',
    'size_t': 'usize',
    'uintptr_t': 'usize',
    'intptr_t': 'isize',
    'float': 'f32',
    'double': 'f64' 
}

KEYWORD = {
    'addrspace',
    'align',
    'and',
    'asm',
    'async',
    'await',
    'break',
    'callconv',
    'catch',
    'comptime',
    'const',
    'continue',
    'defer',
    'else',
    'enum',
    'errdefer',
    'error',
    'export',
    'extern',
    'fn',
    'for',
    'if',
    'inline',
    'linksection',
    'noalias',
    'noinline',
    'nosuspend',
    'opaque',
    'or',
    'orelse',
    'packed',
    'pub',
    'resume',
    'return',
    'struct',
    'suspend',
    'switch',
    'test',
    'threadlocal',
    'try',
    'union',
}

def fix_zig_keyword_name(name:str)->str:
    if name in KEYWORD:
        return f"_{name}_"
    return name

def clean_type_spelling(spelling: str) -> str:
    """清理类型名称中的 struct / union / const 等前缀修饰"""
    return (
        spelling.replace('struct ', '')
        .replace('union ', '')
        .replace('const ', '')
        .strip()
    )


def collect_flattened_fields(record_node, layer_name=None, depth=0, visited=None):
    """递归收集结构体及其多层继承基类的字段，支持展平和层级记录"""
    if visited is None:
        visited = set()
    
    node_id = hash(record_node)
    if node_id in visited:
        return []
    visited.add(node_id)

    base_blocks = []
    for child in record_node.get_children():
        if child.kind == CursorKind.CXX_BASE_SPECIFIER:
            base_type = child.type
            base_decl = base_type.get_declaration()
            b_name = clean_type_spelling(base_type.spelling)
            if base_decl and base_decl.is_definition():
                sub_blocks = collect_flattened_fields(base_decl, layer_name=b_name, depth=depth + 1, visited=visited)
                base_blocks.extend(sub_blocks)

    current_fields = []
    for child in record_node.get_children():
        if child.kind == CursorKind.FIELD_DECL:
            field_name = fix_zig_keyword_name(child.spelling)
            field_type = map_type(child.type)
            if field_name:
                current_fields.append((field_name, field_type))

    result = []
    result.extend(base_blocks)
    
    this_layer_name = layer_name if layer_name else record_node.spelling
    if current_fields or not base_blocks:
        result.append({
            'layer_name': this_layer_name,
            'depth': depth,
            'fields': current_fields
        })

    return result


def map_type(c_type: cindex.Type) -> str:
    """递归将 libclang 的 Type 节点映射为 Zig 类型"""
    # 1. 优先匹配 stdint 别名
    type_name = c_type.spelling.replace('const ', '').strip()
    if type_name in TYPEDEF_OVERRIDE:
        return TYPEDEF_OVERRIDE[type_name]

    # 2. 基本原生数值类型
    if c_type.kind in PRIMITIVE_MAP:
        return PRIMITIVE_MAP[c_type.kind]

    # 3. 定长数组: T[N] -> [N]T
    if c_type.kind == TypeKind.CONSTANTARRAY:
        elem_zig = map_type(c_type.get_array_element_type())
        count = c_type.element_count
        return f'[{count}]{elem_zig}'

    # 4. 指针处理
    if c_type.kind == TypeKind.POINTER:
        pointee = c_type.get_pointee()

        # void* -> ?*anyopaque
        if pointee.kind == TypeKind.VOID:
            return '?*anyopaque'

        # const char* -> ?[*:0]const u8
        if (
            pointee.kind in (TypeKind.CHAR_S, TypeKind.CHAR_U)
            and pointee.is_const_qualified()
        ):
            return '?[*:0]const u8'

        # 二级指针 (例如: Il2CppClass**) -> ?[*]?[*]Il2CppClass
        if pointee.kind == TypeKind.POINTER:
            inner_pointee = pointee.get_pointee()
            inner_name = clean_type_spelling(inner_pointee.spelling)
            inner_name = TYPEDEF_OVERRIDE[inner_name] if inner_name in TYPEDEF_OVERRIDE else inner_name
            return f'?*?[*]{inner_name}'

        # 函数指针
        if pointee.kind in (TypeKind.FUNCTIONPROTO, TypeKind.FUNCTIONNOPROTO):
            ret_type = map_type(pointee.get_result())
            args = [map_type(arg) for arg in pointee.argument_types()]
            return f'?*const fn ({", ".join(args)}) callconv(.c) {ret_type}'

        # 普通结构体指针: ?*T 或 ?*const T
        pointee_name = clean_type_spelling(pointee.spelling)
        if pointee_name in TYPEDEF_OVERRIDE:
            pointee_name = TYPEDEF_OVERRIDE[pointee_name]

        const_str = 'const ' if pointee.is_const_qualified() else ''
        return f'?*{const_str}{pointee_name}'

    # 5. 自定义 Typedef / 结构体名
    return clean_type_spelling(c_type.spelling)


class AstToZigGenerator:
    def __init__(self, filename: str, content: str = None):
        self.filename = filename
        self.content = content
        self.defined_records = set()  # 记录具有完整定义的 struct/union
        self.output_lines = []

    def run(self) -> str:
        index = cindex.Index.create()
        args = ['-x', 'c++', '-std=c++17']

        unsaved = [(self.filename, self.content)] if self.content else []
        tu = index.parse(self.filename, args=args, unsaved_files=unsaved)

        # 检查是否有严重编译错误
        for diag in tu.diagnostics:
            if diag.severity >= cindex.Diagnostic.Error:
                print(f'[Clang Error] {diag.spelling}', file=sys.stderr)

        # 第一遍扫描：收集所有有真正定义的 struct/union 名称
        for node in tu.cursor.get_children():
            if self.is_target_node(node):
                if node.kind in (CursorKind.STRUCT_DECL, CursorKind.UNION_DECL):
                    if node.is_definition():
                        self.defined_records.add(node.spelling)

        # 生成头部导入
        self.emit('const std = @import("std");\n')

        # 第二遍扫描：AST 节点转译
        for node in tu.cursor.get_children():
            if not self.is_target_node(node):
                continue
            self.process_node(node)

        return '\n'.join(self.output_lines)

    def is_target_node(self, node) -> bool:
        """过滤掉标准库引入的冗余节点，只处理目标文件内声明"""
        return node.location.file and node.location.file.name.endswith(self.filename)

    def emit(self, text: str):
        self.output_lines.append(text)

    def process_node(self, node):
        # 1. 结构体与联合体声明
        if node.kind in (CursorKind.STRUCT_DECL, CursorKind.UNION_DECL):
            name = node.spelling
            if not name:
                return

            # 如果只是前置声明 (如 struct MethodInfo;)
            if not node.is_definition():
                # 如果后面没有它的实体定义，说明是不透明指针，生成 opaque
                if name not in self.defined_records:
                    self.emit(f'pub const {name} = opaque {{}};')
                    self.defined_records.add(name)  # 避免多次声明
                return

            # 具有实体定义：生成 extern struct 或 extern union
            decl_kind = (
                'extern struct'
                if node.kind == CursorKind.STRUCT_DECL
                else 'extern union'
            )
            self.emit(f'pub const {name} = {decl_kind} {{')

            # 遍历结构体字段 (支持多层继承展平与层级注释)
            blocks = collect_flattened_fields(node)
            for block in blocks:
                if block['fields']:
                    self.emit(f'    // Layer {block["depth"]}: {block["layer_name"]}')
                    for field_name, field_type in block['fields']:
                        self.emit(f'    {field_name}: {field_type},')

            self.emit('};\n')

        # 2. Typedef 声明
        elif node.kind == CursorKind.TYPEDEF_DECL:
            name = node.spelling
            underlying = node.underlying_typedef_type

            # 函数指针 typedef：void(*Il2CppMethodPointer)()
            if (
                underlying.kind == TypeKind.POINTER
                and underlying.get_pointee().kind
                in (
                    TypeKind.FUNCTIONPROTO,
                    TypeKind.FUNCTIONNOPROTO,
                )
            ):
                fn_type = underlying.get_pointee()
                ret_type = map_type(fn_type.get_result())
                args = [map_type(a) for a in fn_type.argument_types()]
                self.emit(
                    f'pub const {name} = ?*const fn ({", ".join(args)}) callconv(.c) {ret_type};\n'
                )
            else:
                target_type = map_type(underlying)
                self.emit(f'pub const {name} = {target_type};\n')


# ==========================================
# 测试运行
# ==========================================
if __name__ == '__main__':
    f = open('il2cpp.h', 'r')
    c_source = f.read()
    f.close()
    generator = AstToZigGenerator('il2cpp.h', content=c_source)
    zig_code = generator.run()
    # print(zig_code)
    f = open('il2cpp_binding.zig','w')
    f.write(zig_code)
    f.close()

    print('il2cpp绑定转换成功！')

