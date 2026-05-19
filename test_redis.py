# test_redis.py
# Script kiểm tra kết nối và cấu hình Redis
import redis
import sys

REDIS_HOST = 'localhost'
REDIS_PORT = 6378
REDIS_PASSWORD = "YOUR_STRONG_PASSWORD"  # Nếu có password, điền vào đây

try:
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD, decode_responses=True)
    pong = r.ping()
    if pong:
        print('✅ Redis đã hoạt động và kết nối thành công!')
    else:
        print('❌ Không thể kết nối tới Redis.')
        sys.exit(1)
    # Test set/get
    r.set('test_key', 'hello_redis')
    value = r.get('test_key')
    if value == 'hello_redis':
        print('✅ Redis set/get hoạt động đúng!')
    else:
        print('❌ Redis set/get lỗi!')
        sys.exit(1)
except Exception as e:
    print(f'❌ Lỗi khi kết nối hoặc kiểm tra Redis: {e}')
    sys.exit(1)
