from faf import db
from pymysql.cursors import DictCursor

from api.error import ApiException, Error, ErrorCode


def get_select_expressions(fields, field_expression_dict):
    """
    Returns select expressions for all `fields` in `field_expression_dict`.
    Example usage::

        fields = ['id', 'timestamp']
        field_expression_dict = {
            'id': 'map.uid',
            'timestamp': 'UNIX_TIMESTAMP(t.date)',
            'likes': 'feature.likes'
        }

        get_select_expressions(fields, field_expression_dict)

    Result::

        "map.uid as id, UNIX_TIMESTAMP(t.date) as timestamp"

    :param fields: the list of fields to select from `field_expression_dict`. If `None` or empty, all fields will be
    returned
    :param field_expression_dict: a dictionary mapping field names to select expressions. The expressions must not
    contain "as" clauses since they will be appended using the `field` (see example above)
    :return: a select expressions string (see example above)
    """
    if not fields:
        fields = field_expression_dict.keys()

    field_selects = []
    for field in fields:
        if field in field_expression_dict:
            field_selects.append("{} AS `{}`".format(field_expression_dict[field], field))

    return ', '.join(field_selects)


def get_order_by(sort_expression, valid_fields):
    """
    Converts the `sort_expression` into an "order by" if all fields are in `field_expression_dict`
    Example usage::

        sort_expression = 'likes,-timestamp'
        field_expression_dict = {
            'id': 'map.uid',
            'timestamp': 'UNIX_TIMESTAMP(t.date)',
            'likes': 'feature.likes'
        }

        get_order_by(sort_expression, field_expression_dict)

    Result::

        "ORDER BY likes ASC, timestamp DESC"

    :param sort_expression: a json-api conform sort expression (see example above)
    :param valid_fields: a list of valid sort fields
    :return: an MySQL conform ORDER BY string (see example above) or an empty string if `sort_expression` is None or
    empty
    """
    if not sort_expression:
        return ''

    sort_expressions = sort_expression.split(',')

    order_bys = []

    for expression in sort_expressions:
        if not expression or expression == '-':
            continue

        if expression[0] == '-':
            order = 'DESC'
            column = expression[1:]
        else:
            order = 'ASC'
            column = expression

        if column not in valid_fields:
            raise ApiException([Error(ErrorCode.QUERY_INVALID_SORT_FIELD, column)])

        order_bys.append('`{}` {}'.format(column, order))

    if not order_bys:
        return ''

    return 'ORDER BY {}'.format(', '.join(order_bys))


def get_limit(page, limit):
    page = int(page)
    limit = int(limit)
    return 'LIMIT {}, {}'.format((page - 1) * limit, limit)


def fetch_data(schema, table, root_select_expression_dict, max_page_size, request, where='', where_extension='',
               args=None, many=True,
               enricher=None, sort=None, limit=True, **nested_expression_dict):
    """ Fetches data in an JSON-API conforming way.

    :param schema: the marshmallow schema to use for serialization, provided by faftools: https://github.com/FAForever/faftools/tree/develop/faf/api 
    :param table: the table to select the data from (or any FROM expression, without the FROM)
    :param root_select_expression_dict: a dictionary that maps API field names to select expressions
    :param max_page_size: max number of items per page
    :param request: the flask HTTP request
    :param where: additional WHERE clauses, without the WHERE
    :param args: arguments to use when building the SQL query (e.g. ``where="id = %(id)s", args=dict(id=id)``
    :param many: ``True`` for selecting many entries, ``False`` for single entries
    :param enricher: an option function to apply to each item BEFORE it's dumped using the schema
    :param sort: order the query by given column name in asc order, prefix with '-' for desc order
    :param nested_expression_dict: dict of nested objects to be found in select_expression_dict e.g.
        nested_expression_dict = {'nest_atr_name' : { 'nest_atr_key' : 'nest_atr_value'}}
    """
    requested_fields = request.values.get('fields[{}]'.format(schema.Meta.type_))

    if not sort:
        sort = request.values.get('sort')

    select_dict = {**root_select_expression_dict}
    for nested_dict in nested_expression_dict.values():
        select_dict.update(nested_dict)

    # Sanitize fields
    if requested_fields:
        fields = [field for field in requested_fields.split(',') if field in select_dict.keys()]
        nested_fields = [field for field in requested_fields.split(',') if field in nested_expression_dict.keys()]
        for nested_field in nested_fields:
            fields.extend([*nested_expression_dict[nested_field]])
    else:
        fields = [*select_dict.keys()]

    id_selected = True
    if 'id' not in fields:
        # ID must always be selected
        fields.append('id')
        id_selected = False

    select_expressions = get_select_expressions(fields, select_dict)

    limit_expression = ''
    order_by_expression = ''
    if many:
        page, page_size = get_page_attributes(max_page_size, request)
        if limit:
            limit_expression = get_limit(page, page_size)
        order_by_expression = get_order_by(sort, fields)

    if where:
        where = "WHERE {}".format(where)

    if where_extension:
        where = where + " " + where_extension

    with db.connection:
        cursor = db.connection.cursor(DictCursor)
        cursor.execute("SELECT {} FROM {} {} {} {}"
                       .format(select_expressions, table, where, order_by_expression, limit_expression),
                       args)

        if many:
            result = cursor.fetchall()
        else:
            result = cursor.fetchone()

    if enricher:
        if many:
            for item in result:
                enricher(item)
        elif result:
            enricher(result)

    data = schema.dump(result, many=many).data

    # TODO `id` is treated specially, that means it's put into ['data'] and NOT into ['attributes']
    # Schema().loads() however only returns ['attributes'] - and I found no way to either change that, or add 'id'
    # to ['attributes']. If there really is no clean solution, we either can't use loads(), or we use this ugly code.
    if id_selected:
        if many:
            for item in data['data']:
                if 'attributes' not in item:
                    break
                item['attributes']['id'] = item['id']
        elif 'id' in data['data'] and 'attributes' in data['data']:
            data['data']['attributes']['id'] = data['data']['id']

    return data


def get_page_attributes(max_page_size, request):
    raw_page_size = request.values.get('page[size]', max_page_size)
    try:
        page_size = int(raw_page_size)
        if page_size > max_page_size:
            raise ApiException([Error(ErrorCode.QUERY_INVALID_PAGE_SIZE, page_size)])
    except ValueError:
        raise ApiException([Error(ErrorCode.QUERY_INVALID_PAGE_SIZE, raw_page_size)])

    raw_page = request.values.get('page[number]', 1)
    try:
        page = int(raw_page)
        if page < 1:
            raise ApiException([Error(ErrorCode.QUERY_INVALID_PAGE_NUMBER, page)])
    except ValueError:
        raise ApiException([Error(ErrorCode.QUERY_INVALID_PAGE_NUMBER, raw_page)])

    return page, page_size
